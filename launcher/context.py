"""ConversationContext 装配。

**这个文件就是 lumi.py 的"接口契约实现"**：`conversation.py:385` 的
`ConversationContext` dataclass 里那些没有默认值的字段，全部必须在这里被填上。
上游把这些函数留在未开源的 lumi.py 里，本层把它们实现出来
（文本处理/工具/锚点在 helpers.py，视觉与画画是显式占位）。

字段分四类（与 dataclass 的注释一致）：
  1. 可变全局（引用语义）：turn_metrics / history / context_slot / 各种锁与事件
  2. 句柄：bus
  3. lazy-bound 回调：bridge 的 get_*（启动后才赋值 → 用 lambda 实时读 RUNTIME）
  4. boot 常量与函数回调
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import MISSING, fields

from conversation import ConversationContext

from launcher import helpers as H
from launcher.registry import RUNTIME

# 允许为 None 的字段：这些是"可摘除能力"，conversation 内部对它们都有
# `if ctx.memory_runtime:` 之类的守卫（见 conversation.py:678 / 746 / 1036）。
OPTIONAL_NONE = {"memory_runtime"}

# 主动说话/旁观的提示词（追加到 system prompt 之后）
# TODO(prompt-tuning)：上游这些提示词属于"角色灵魂"的一部分（未开源），
# 这里是功能等价的最小实现——保证 proactive 路径能跑通，不代表最终语气。
PROACTIVE_PROMPT_OPENING = (
    "## 现在\n"
    "直播刚开场，没有人跟你说话。主动说一句简短的问候或开场白，一句话，别超过 30 字。"
)
PROACTIVE_PROMPT_GAMING = (
    "## 现在\n"
    "你们正在一起打游戏。主动说一句和当前局面相关的解说或吐槽，一句话，别超过 30 字。"
)
PROACTIVE_PROMPT_CONTINUE = (
    "## 现在\n"
    "已经安静了一会儿。主动接一句新话题或吐槽，一句话，别超过 30 字，不要重复之前说过的内容。"
)
SPECTATOR_PROMPT_GAMING = (
    "## 你的身份：旁观者\n"
    "这一轮不是你操作游戏，你没有操作权、也不要调用任何动作工具。"
    "你仍然是主播：照样只说一句短话，可以吐槽、接梗、回应弹幕。"
)


def _lazy_bridge(name: str):
    return lambda: RUNTIME.get_bridge(name)


def _lazy_ready(name: str):
    return lambda: RUNTIME.is_game_ready(name)


def build_context(*, bus, scheduler, fast_brains, speaker_configs,
                  cable_indices, active_speakers, memory_runtime=None,
                  session_id: str = "", log_event=None) -> ConversationContext:
    """按 conversation.ConversationContext 的字段表逐项装配。"""
    log_event = log_event or (lambda msg: print(msg, flush=True))

    fields = dict(
        # ── 1. 可变全局（引用语义传递）─────────────────────────────────
        turn_metrics={},
        history=[],                       # 快脑历史（单角色兼容用；双角色走 fast_brains）
        # ★ context_slot 必须预置 activity_type：conversation.py:654 用的是
        #   `ctx.context_slot["activity_type"]`（下标访问，不是 .get），
        #   缺这个键会在每一轮直接 KeyError。其余键（game_state_snapshot /
        #   _buckshot_situation）都是 .get(...)，可以不预置。
        context_slot={"activity_type": "chatting"},
        slot_lock=threading.Lock(),
        speaking_lock=threading.Lock(),
        slow_brain_trigger=threading.Event(),
        recent_lumi_outputs=deque(maxlen=20),

        # ── 2. 句柄 ────────────────────────────────────────────────────
        bus=bus,

        # ── 3. lazy-bound 游戏桥接回调（启动后才赋值 → 每次实时读）──────
        touch_last_message=lambda: RUNTIME.touch_viewer_message(time.time()),
        get_buckshot_bridge=_lazy_bridge("buckshot"),
        get_buckshot_game_ready=_lazy_ready("buckshot"),
        get_wordle_bridge=_lazy_bridge("wordle"),
        get_wordle_game_ready=_lazy_ready("wordle"),
        get_handle_bridge=_lazy_bridge("handle"),
        get_handle_game_ready=_lazy_ready("handle"),
        get_terraria_bridge=_lazy_bridge("terraria"),
        get_terraria_game_ready=_lazy_ready("terraria"),
        get_kr_bridge=_lazy_bridge("kr"),
        get_kr_game_ready=_lazy_ready("kr"),

        # ── 4. boot 常量 ───────────────────────────────────────────────
        fast_draw_tool={},
        proactive_prompt_opening=PROACTIVE_PROMPT_OPENING,
        proactive_prompt_gaming=PROACTIVE_PROMPT_GAMING,
        spectator_prompt_gaming=SPECTATOR_PROMPT_GAMING,
        proactive_prompt_continue=PROACTIVE_PROMPT_CONTINUE,

        # ── 运行时开关（lambda 实时读，便于 argparse 之后改）───────────
        get_enable_drawing=lambda: RUNTIME.enable_drawing,
        get_enable_tts=lambda: RUNTIME.enable_tts,

        # ── 函数回调（原先实现在 lumi.py 里）────────────────────────────
        log_event=log_event,
        parse_emotion=H.parse_emotion,
        strip_stage_directions=H.strip_stage_directions,
        build_slot_prompt=H.build_slot_prompt,
        capture_screen=H.capture_screen,                   # TODO(unopened)
        log_turn=H.log_turn,
        detect_activity_switch=H.detect_activity_switch,   # 保守 no-op
        extract_draw_subject=H.extract_draw_subject,       # TODO(unopened)
        mark_drawing_started=H.mark_drawing_started,       # TODO(unopened)
        on_draw_complete=H.on_draw_complete,               # TODO(unopened)
        get_draw_stage_offer=H.get_draw_stage_offer,       # TODO(unopened)
        execute_fast_brain_tools=H.execute_fast_brain_tools,
        interrupt_monitor=H.interrupt_monitor,
        kr_build_anchor_msg=H.kr_build_anchor_msg,
        terraria_build_anchor_msg=H.terraria_build_anchor_msg,

        # ── Step 2 双角色字段 ──────────────────────────────────────────
        active_speakers=list(active_speakers),
        fast_brains=dict(fast_brains),
        scheduler=scheduler,
        speaker_configs=dict(speaker_configs),
        cable_indices=dict(cable_indices),
        memory_runtime=memory_runtime,
        get_session_id=lambda: session_id,
        get_current_game_controller=lambda: RUNTIME.current_game_controller,
    )
    return ConversationContext(**fields)


def assert_context_complete(ctx: ConversationContext):
    """装配断言：所有无默认值字段都必须被填（少数可选能力允许为 None）。

    这是把"漏填字段"从运行期报错提前到启动期——上游那种跑到一半才发现
    `ctx.xxx is None` 的体验很差。测试 tests/test_launcher_assembly.py 也用它。
    """
    missing = [
        f.name for f in fields(ctx)
        if f.default is MISSING and f.default_factory is MISSING
        and f.name not in OPTIONAL_NONE
        and getattr(ctx, f.name) is None
    ]
    if missing:
        raise ValueError(f"ConversationContext 装配不完整，缺字段：{missing}")
    if not ctx.active_speakers:
        raise ValueError("active_speakers 不能为空")
    if ctx.scheduler is None:
        raise ValueError("scheduler 未注入")
    if ctx.fast_brains is None or set(ctx.fast_brains) != set(ctx.active_speakers):
        raise ValueError(
            f"fast_brains 的键 {list(ctx.fast_brains or {})} 与 "
            f"active_speakers {ctx.active_speakers} 不一致"
        )
    if set(ctx.cable_indices.keys()) - set(ctx.active_speakers):
        raise ValueError(
            f"cable_indices 的键 {list(ctx.cable_indices)} 与 "
            f"active_speakers {ctx.active_speakers} 不匹配"
        )
    return True
