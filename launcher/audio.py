"""声卡枚举与语音层初始化（realtime_chat / lumi_tts / lumi_asr）。

设计要点：
- **重依赖全部惰性导入**：pyaudiowpatch / dashscope / websockets 只在真正要发声时
  才 import，所以 `--dry-run` 与 `--no-audio` 在没装这些包的机器上也能跑装配。
- **设备解析是"模糊匹配"**：角色配置里存的是声卡名的关键字（如 `CABLE Input`），
  这里把关键字映射成设备索引；匹配不到就回退默认设备并在日志里说清楚。
"""
from __future__ import annotations

import re

from pathlib import Path

from launcher.config import ROOT


def open_pa(log_fn):
    """打开 PyAudio（WASAPI）。失败返回 None —— 上层据此降级为"无声运行"。"""
    try:
        import pyaudiowpatch as pyaudio
    except Exception as e:
        log_fn(f"[音频] 未安装 pyaudiowpatch，跳过音频：{e}")
        return None
    try:
        return pyaudio.PyAudio()
    except Exception as e:
        log_fn(f"[音频] PyAudio 初始化失败，跳过音频：{e}")
        return None


def find_output_device(pa, keyword: str, log_fn):
    """按关键字模糊匹配输出设备，返回 index；找不到返回 None。"""
    if pa is None or not keyword:
        return None
    kw = keyword.strip().lower()
    try:
        count = pa.get_device_count()
    except Exception:
        return None
    fallback = None
    for i in range(count):
        try:
            info = pa.get_device_info_by_index(i)
        except Exception:
            continue
        if int(info.get("maxOutputChannels", 0)) <= 0:
            continue
        name = str(info.get("name", ""))
        if kw in name.lower():
            return i
        if fallback is None and "cable input" in name.lower():
            fallback = i
    if fallback is not None:
        log_fn(f"[音频] 关键字 '{keyword}' 未精确匹配，回退到 CABLE Input (index={fallback})")
    else:
        log_fn(f"[音频] 关键字 '{keyword}' 未匹配到输出设备，将用系统默认设备")
    return fallback


def resolve_devices(pa, characters, log_fn, keyword_override: str = "") -> tuple[dict, int | None]:
    """按角色解析虚拟声卡索引：{角色: index}。

    `keyword_override` 非空时，所有角色都用它匹配同一个设备
    （用于"没装虚拟声卡也想先听到声音"的场景——代价是两个角色混到同一路输出）。
    """
    from voice_config import get_speaker_config
    device_map, monitor = {}, None
    for name in characters:
        if keyword_override:
            kw = keyword_override
        else:
            try:
                cfg = get_speaker_config(name)
                kw = getattr(cfg, "audio_cable_keyword", "")
            except Exception as e:
                log_fn(f"[音频] 角色 {name} 配置读取失败：{e}")
                continue
        idx = find_output_device(pa, kw, log_fn)
        if idx is not None:
            device_map[name] = idx
            log_fn(f"[音频] {name} → 声卡 index={idx}（关键字 '{kw}'）")
        else:
            log_fn(f"[音频] {name} → 匹配不到关键字 '{kw}'，将把音频落盘而不是播放"
                   f"（可加 --audio-device-keyword 试试你机器的设备名）")
    return device_map, monitor


def _read_persona_manifest(name: str, log_fn) -> str:
    """读取实时链路用的净化人设，并按独播/双角色裁剪 [[DUAL_ONLY]] 标记。

    文件不存在时返回空串：realtime_chat 会用角色默认嗓音，仅少了人设。
    """
    from voice_config import get_speaker_config
    try:
        cfg = get_speaker_config(name)
        rel = getattr(cfg, "realtime_character_manifest_file", "") or ""
    except Exception:
        rel = ""
    candidates = [ROOT / rel] if rel else []
    candidates.append(ROOT / "persona" / f"{name}.md")
    for path in candidates:
        try:
            if path.exists():
                text = path.read_text(encoding="utf-8")
                # 复用上游的标记裁剪（否则方括号标记会被端到端当文字念出来）
                import fast_brain
                is_dual = len(get_speaker_config_cache()) > 1
                return fast_brain._apply_persona_mode(text, is_dual)
        except Exception as e:
            log_fn(f"[人设] {path.name} 读取失败：{e}")
    log_fn(f"[人设] 未找到 {name} 的人设文件（persona/{name}.md），实时链路将不带人设")
    return ""


_speaker_config_cache: dict = {}


def get_speaker_config_cache() -> dict:
    return _speaker_config_cache


def init_realtime_layer(*, cfg, bus, log_fn, pa):
    """初始化端到端语音会话池（doubao SC2.0）。

    ⚠️ 只会话池本身需要凭证；文本架构下不调用本函数。
    """
    import os
    import realtime_chat
    from voice_config import get_speaker_config

    app_id = os.environ.get("VOLC_DIALOG_APP_ID", "")
    access_key = os.environ.get("VOLC_DIALOG_ACCESS_KEY", "")
    if not app_id or not access_key:
        raise RuntimeError("端到端架构需要 VOLC_DIALOG_APP_ID / VOLC_DIALOG_ACCESS_KEY")

    realtime_chat.init(log_fn=log_fn, pa_instance=pa, event_bus=bus,
                       app_id=app_id, access_key=access_key)

    sessions = {}
    for name in cfg.characters:
        sc = get_speaker_config(name)
        sessions[name] = {
            "character_manifest": _read_persona_manifest(name, log_fn),
            "voice_id": getattr(sc, "realtime_voice_id", "") or "",
        }
    # 多角色形态：start_session 会在全部 session 起好后自动跑一遍 prime
    #（不 prime 的 session 首次发 ChatTTSText 会被服务端静默吞掉）
    realtime_chat.start_session(sessions=sessions, active_speakers=list(cfg.characters))
    log_fn(f"[语音] 端到端会话池已起 {len(sessions)} 条：{list(sessions)}")
    return realtime_chat


def init_tts_layer(*, cfg, bus, log_fn, pa, monitor_index=None):
    """初始化独立 TTS（文本架构）。注入 llm_client / brand_params 等依赖。"""
    import fast_brain
    import lumi_tts

    # 句末标点：lumi_tts 用它决定"何时把这一句切出去合成"
    sentence_endings = re.compile(r"[。！？!?…；;\n]+")

    def trigger_expression(*a, **k):
        # 上游这里驱动 Live2D 表情（图层未开源）→ 显式占位
        return None

    lumi_tts.init(
        llm_client=fast_brain.llm_client,
        llm_model=fast_brain.LLM_MODEL,
        brand_params_fn=fast_brain._brand_params,
        resolve_call_target_fn=fast_brain.resolve_call_target,
        trigger_expression_fn=trigger_expression,
        pa_instance=pa,
        sentence_endings=sentence_endings,
        log_fn=log_fn,
        monitor_device_index=monitor_index,
        enable_subtitle_obs=not cfg.dry_run,
        enable_subtitle_desktop=False,
        event_bus=bus,
    )
    log_fn("[语音] 独立 TTS（CosyVoice）已初始化")
    return lumi_tts


def init_asr_layer(*, cfg, on_sentence_end, log_fn):
    """初始化流式 ASR（DashScope fun-asr-realtime）。"""
    from lumi_asr import StreamingAsr
    asr = StreamingAsr(on_sentence_end=on_sentence_end)
    asr.start()
    log_fn("[语音] 流式 ASR 已启动（连接轮换 + 预备连接续租）")
    return asr


def init_duplex_layer(*, cfg, characters, pa, cable_indices, log_fn):
    """初始化新版端到端实时语音（duplex，JSON 事件协议）。

    与 `init_realtime_layer`（旧二进制协议）的区别：凭证只要一个 API Key，
    且本层只把它当「嗓子」用——台词仍由快脑生成（A 模式）。

    `--duplex-wav-dir` 非空时，即便没有声卡也会把音频落盘成 PCM，
    便于无声环境下验证整条链路。
    """
    from launcher.duplex_client import DEFAULT_VOICE, resolve_api_key
    from launcher.duplex_voice import DuplexVoice
    from voice_config import get_speaker_config

    api_key = resolve_api_key()
    if not api_key:
        raise RuntimeError("duplex 架构需要 API Key：设 VOLC_DUPLEX_API_KEY"
                           "（或 VOLC_DIALOG_ACCESS_KEY），申请见 docs/CREDENTIALS.md")

    def _voice_of(name: str) -> str:
        sc = get_speaker_config(name)
        vid = (getattr(sc, "realtime_voice_id", "") or "").strip()
        # 仓库里的占位值不算数，回退到协议默认音色
        if not vid or vid.startswith("REPLACE_WITH"):
            return getattr(cfg, "duplex_voice", "") or DEFAULT_VOICE
        return vid

    def _instructions_of(name: str) -> str:
        """把角色人设喂给会话（它只用于语音风格/称呼，台词内容由快脑决定）。"""
        try:
            path = ROOT / "persona" / f"{name}.md"
            if path.exists():
                text = path.read_text(encoding="utf-8")
                import re as _re
                m = _re.search(r"```\n(.*?)```", text, _re.DOTALL)
                body = (m.group(1) if m else text).strip()
                return body[:1500]
        except Exception as e:
            log_fn(f"[人设] duplex 读取 {name} 人设失败：{e}")
        return ""

    voice = DuplexVoice(
        api_key=api_key, characters=characters, log_fn=log_fn, pa=pa,
        cable_indices=cable_indices, voice_of=_voice_of,
        model=getattr(cfg, "duplex_model", "") or None,
        instructions_of=_instructions_of,
        wav_dir=getattr(cfg, "duplex_wav_dir", "") or None,
    )
    if voice.start():
        log_fn(f"[语音] duplex 会话池已起 {len(voice.sessions)} 条：{list(voice.sessions)}")
    return voice
