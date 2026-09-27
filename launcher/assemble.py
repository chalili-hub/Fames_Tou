"""装配：把零件拼成一场能跑的直播。

**这一步是本层的核心价值**——上游把 `lumi.py` 从仓库里拿掉了，
于是仓库里留下几十个"有实现、没调用方"的函数；本文件负责把它们接起来。

★ 两个最容易漏、漏了会让功能静默失效的装配步骤（都写在 build_engine 里）：
  1. `arbiter.configure(event_bus=bus)`：仲裁器的模块级单例创建时**没有总线**，
     不配置的话 `speech_output_*` 事件根本不会上总线 → 复盘报告永远是 0。
  2. `arbiter.set_cooldown(cfg.cooldown)`：连麦冷却默认是 0（关闭，为向后兼容），
     不显式打开，这个策略就永远不生效。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from launcher import audio as A
from launcher import inputs as I
from launcher.config import LauncherConfig
from launcher.context import assert_context_complete, build_context
from launcher.registry import RUNTIME


@dataclass
class Engine:
    cfg: LauncherConfig
    bus: object = None
    state: object = None
    scheduler: object = None
    arbiter: object = None
    analytics: object = None
    memory: object = None
    context: object = None
    pa: object = None
    cable_indices: dict = field(default_factory=dict)
    source: object = None
    mic: object = None
    asr: object = None
    realtime: object = None
    duplex: object = None
    tts: object = None
    games: dict = field(default_factory=dict)
    turns_played: int = 0


def _start_game_bridge(cfg, log_fn):
    """（已由 launcher.games.start_game 取代，保留此函数仅为兼容旧引用。）

    真正的接线在 launcher/games.py：起桥接 → 注册到 RUNTIME → 切状态机到 PLAYING_*。
    """
    return {}


def _apply_model_choice(cfg, log_fn) -> None:
    """选择快脑模型：既接受注册表里的 key，也接受**任意 model id / 推理接入点**。

    为什么需要后者：方舟有两种调用方式——
    ① model id 直连（如 `doubao-seed-2-0-lite-260428`，需账号开通该模型）；
    ② 推理接入点（`ep-xxxxxxxx`）。
    上游的 `LLM_MODELS` 只登记了少数 model id，且 `set_llm_model` 会校验 key，
    于是"我有接入点但代码不认"就成了拦路虎。这里允许直接传裸 id：
    临时把它登记成 `custom` 条目再选中。
    """
    from fast_brain import LLM_MODELS, set_llm_model
    if cfg.model in LLM_MODELS:
        set_llm_model(cfg.model)
        log_fn(f"[装配] 快脑模型切换为 {cfg.model}（注册表条目）")
        return
    try:
        ark_client = LLM_MODELS["2.0-lite"][1]      # 复用同一个方舟客户端
    except Exception:
        ark_client = None
    LLM_MODELS["custom"] = ("自定义模型/接入点", ark_client, cfg.model, "doubao")
    set_llm_model("custom")
    log_fn(f"[装配] 快脑使用自定义 model id / 接入点：{cfg.model}"
           f"（若报 ModelNotOpen，说明该模型未在方舟开通）")


def build_engine(cfg: LauncherConfig, *, log_fn=print) -> Engine:
    from event_bus import EventBus
    from state_machine import StateMachine
    from speaker_scheduler import SpeakerScheduler
    import speech_output_arbiter as soa
    from stream_analytics import StreamAnalytics
    from voice_config import get_speaker_config
    from fast_brain import FastBrain, set_llm_model

    eng = Engine(cfg=cfg)

    # ── 运行时状态 ────────────────────────────────────────────────────
    # ★ 运行架构必须在这里落到单一真相源上：`lumi_tts._make_emitter()` 与
    #   `speak()` 都查 `run_architecture`，不设置的话架构会一直停在默认 "text"，
    #   于是 realtime 架构下会错误地去构造 CosyVoice 发声器（实测踩过）。
    import run_architecture
    run_architecture.set_architecture(cfg.arch)
    log_fn(f"[装配] 运行架构 = {run_architecture.get_architecture()}"
           f"（独立发声={run_architecture.use_independent_tts()}，"
           f"duplex={run_architecture.use_duplex()}）")

    RUNTIME.enable_drawing = bool(cfg.enable_drawing)
    RUNTIME.enable_tts = bool(cfg.enable_tts)
    RUNTIME.enable_audio = bool(cfg.enable_audio)
    RUNTIME.session_id = cfg.session_id or f"s{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    RUNTIME.started_at = time.time()

    if cfg.model:
        _apply_model_choice(cfg, log_fn)

    # ── 协调骨架 ──────────────────────────────────────────────────────
    eng.bus = EventBus()
    eng.state = StateMachine(eng.bus)
    eng.scheduler = SpeakerScheduler(active_speakers=list(cfg.characters))

    eng.arbiter = soa.arbiter
    eng.arbiter.configure(event_bus=eng.bus, log_fn=log_fn)          # ★ 关键 1
    eng.arbiter.set_cooldown(cfg.cooldown)                            # ★ 关键 2
    log_fn(f"[装配] 仲裁器已接总线；连麦冷却 = {eng.arbiter.cooldown_s()}s"
           f"{'（关闭）' if cfg.cooldown <= 0 else ''}")

    eng.analytics = StreamAnalytics(event_bus=eng.bus, active_speakers=list(cfg.characters))
    log_fn("[装配] 可观测性模块已挂上总线（纯订阅者，协调层无需为它改动）")

    # ── 记忆 ──────────────────────────────────────────────────────────
    if not cfg.no_memory:
        try:
            from memory.runtime import MemoryRuntime
            eng.memory = MemoryRuntime(cfg.db_path)
            log_fn(f"[装配] 长期记忆已就绪：{cfg.db_path}")
        except Exception as e:
            eng.memory = None
            log_fn(f"[装配] 记忆初始化失败（降级为无记忆）：{e}")

    # ── 角色大脑（每角色一份独立 history）──────────────────────────────
    # 人设文件缺失时先生成占位，否则 FastBrain.__init__ 会直接 FileNotFoundError
    from launcher.persona import ensure_persona_file
    for name in cfg.characters:
        ensure_persona_file(name, log_fn)
    brains = {name: FastBrain(name, get_speaker_config(name)) for name in cfg.characters}
    speaker_configs = {name: get_speaker_config(name) for name in cfg.characters}

    # ── 音频与语音层 ──────────────────────────────────────────────────
    if cfg.enable_audio and not cfg.dry_run:
        eng.pa = A.open_pa(log_fn)
        if eng.pa is not None:
            eng.cable_indices, monitor = A.resolve_devices(
                eng.pa, cfg.characters, log_fn,
                keyword_override=getattr(cfg, "audio_device_keyword", "") or "")
    else:
        log_fn("[装配] --no-audio/干跑：不打开音频设备")

    if not cfg.dry_run:
        if cfg.arch == "duplex":
            # ★ 新版端到端实时语音（A 模式）：台词仍由快脑生成，
            #   本层只把「发声」接到 duplex 会话上（注入发声器工厂）。
            eng.duplex = A.init_duplex_layer(
                cfg=cfg, characters=cfg.characters, pa=eng.pa,
                cable_indices=eng.cable_indices, log_fn=log_fn,
            )
            if eng.duplex is not None and eng.duplex.sessions:
                import lumi_tts
                lumi_tts.set_emitter_factory(eng.duplex.make_emitter)
                log_fn(f"[语音] duplex 发声器已注入："
                       f"角色 {list(eng.duplex.sessions)} 的播报将走会话合成")
            else:
                log_fn("✗ [语音] duplex 会话未就绪：先跑 "
                       "`python -m launcher.duplex_client --text 测试` 验证凭证")
            # 与 realtime 一样：speak() 需要 lumi_tts 注入的 _llm_client/_sentence_endings
            eng.tts = A.init_tts_layer(cfg=cfg, bus=eng.bus, log_fn=log_fn, pa=eng.pa)
            log_fn("[语音] lumi_tts 依赖已注入（duplex 架构下发声走会话合成）")
        elif cfg.arch == "realtime":
            eng.realtime = A.init_realtime_layer(cfg=cfg, bus=eng.bus, log_fn=log_fn, pa=eng.pa)
            if eng.realtime is not None:
                eng.realtime.set_output_devices(device_map=eng.cable_indices)
                if cfg.enable_audio:
                    eng.realtime.start_audio_output()
                    log_fn("[语音] 端到端音频输出线程已启动")
            # ★ 端到端架构**同样必须**调 lumi_tts.init：
            #   `lumi_tts.speak()` 会用注入进去的 `_llm_client` / `_sentence_endings`
            #   （lumi_tts.py:859 / :1025），不初始化就是 None → 第一次开口就 AttributeError。
            #   端到端下它只是把发声委托给会话（_make_emitter → BorrowE2EEmitter），
            #   `cosyvoice_tts.init(pa_instance=...)` 在未发声时无副作用（源码注释亦已说明）。
            eng.tts = A.init_tts_layer(cfg=cfg, bus=eng.bus, log_fn=log_fn, pa=eng.pa)
            log_fn("[语音] lumi_tts 依赖已注入（端到端架构下发声借用会话）")
        elif cfg.enable_tts:
            eng.tts = A.init_tts_layer(cfg=cfg, bus=eng.bus, log_fn=log_fn, pa=eng.pa)

    # ── 上下文装配（35 个字段）────────────────────────────────────────
    eng.context = build_context(
        bus=eng.bus, scheduler=eng.scheduler, fast_brains=brains,
        speaker_configs=speaker_configs, cable_indices=eng.cable_indices,
        active_speakers=cfg.characters, memory_runtime=eng.memory,
        session_id=RUNTIME.session_id, log_event=log_fn,
    )
    assert_context_complete(eng.context)
    log_fn(f"[装配] ConversationContext 已完成（{len(eng.context.__dataclass_fields__)} 个字段），"
           f"角色 {cfg.characters}，session={RUNTIME.session_id}")

    # ── 输入源与麦克风 ────────────────────────────────────────────────
    eng.source = I.build_source(cfg, scheduler=eng.scheduler,
                                analytics=eng.analytics, log_fn=log_fn)

    # ASR / 麦克风线程**只服务文本架构**：
    #   · text     → 靠 lumi_asr 把语音转文字；
    #   · duplex   → "耳朵"走会话自己的 input_audio_buffer + 转写事件；
    #   · realtime → 旧协议自带通路。
    # 若在非文本架构也起 lumi_asr，它会拿 DashScope key 去连（duplex 下那只是占位值），
    # 结果刷一屏 `fun-asr 错误: Unauthorized`（实测踩过）。
    if (not cfg.dry_run and cfg.enable_audio and eng.pa is not None
            and cfg.arch == "text"):
        try:
            eng.asr = A.init_asr_layer(cfg=cfg, on_sentence_end=None, log_fn=log_fn)
            eng.mic = I.MicPump(pa=eng.pa, asr=eng.asr, scheduler=eng.scheduler,
                                analytics=eng.analytics,
                                vad=__import__("launcher.helpers", fromlist=["EnergyVAD"]).EnergyVAD(),
                                log_fn=log_fn).start()
        except Exception as e:
            log_fn(f"[语音] ASR/麦克风链路未启用（降级）：{e}")

    # ── 游戏桥接（必须在状态切换之前起：桥接订阅 state_changed，
    #    只有收到 PLAYING_BUCKSHOT 才会打开决策门控）────────────────────
    from launcher import games as G
    eng.games = G.start_game(cfg=cfg, bus=eng.bus, state=eng.state, log_fn=log_fn)

    # ── 状态机开播 ────────────────────────────────────────────────────
    from state_machine import State
    eng.state.transition_to(State.OPENING)
    eng.state.transition_to(State.CHATTING)
    eng.scheduler.reset_rotation(cfg.characters[0])
    log_fn(f"[开播] 状态 IDLE→OPENING→CHATTING，由 {cfg.characters[0]} 先开口")

    # 有游戏段落时进入 PLAYING_*（走 TRANSITIONING，符合状态机的活动态约束）
    if "buckshot" in eng.games:
        eng.state.transition_to(State.TRANSITIONING, metadata={"to": "PLAYING_BUCKSHOT"})
        eng.state.transition_to(State.PLAYING_BUCKSHOT)
        log_fn("[游戏] 状态机进入 PLAYING_BUCKSHOT → 桥接决策门控已打开")
    return eng
