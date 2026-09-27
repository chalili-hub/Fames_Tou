"""ConversationContext 要求的那些"小函数"实现。

上游把 conversation.py 从 lumi.py 里拆出来时，把这些函数留在 lumi.py 里、
只通过 ConversationContext 以回调方式引用。它们分三类：

1. 文本处理：parse_emotion / strip_stage_directions / build_slot_prompt
2. 工具执行：execute_fast_brain_tools（把快脑的 tool_call 落到游戏桥接）
3. 打断与画画的接入点：interrupt_monitor / capture_screen / 画画四件套

⚠️ 属于「上游未开源功能」的（视觉截图、画画）在这里是显式占位实现，
   一律带 ``TODO(unopened)`` 标记，不会假装已实现。
"""
from __future__ import annotations

import base64
import re
import threading
import time

# 轻量情绪词表：上游已把情绪标签移到 emotion_sidecar，这里只做兼容解析。
_EMOTION_TAGS = ("开心", "难过", "生气", "惊讶", "害羞", "得意", "无奈", "平静", "好奇", "尴尬")
_LEADING_TAG = re.compile(r"^\s*[\[【(（]\s*([^\]】)）]{1,6})\s*[\]】)）]\s*")
_PAREN_PAIR = re.compile(r"[（(][^（()）]*[)）]")


def parse_emotion(text: str):
    """从回复文本里解析情绪标签，返回 (标签, 去掉标签后的文本)。

    兼容 ``[开心]`` / ``【开心】`` / ``(开心)`` 前缀；无标签返回 ("", 原文)。
    """
    text = text or ""
    m = _LEADING_TAG.match(text)
    if m and m.group(1) in _EMOTION_TAGS:
        tag = m.group(1)
        body = text[m.end():]
        # 顺手去掉模型可能追加的英文标签形式，如 "开心" 后面紧跟的括号
        return tag, body
    return "", text


def strip_stage_directions(text: str) -> str:
    """去掉舞台提示/动作描写（成对的圆括号、方括号旁白前缀）。"""
    text = text or ""
    text = _PAREN_PAIR.sub("", text)
    text = re.sub(r"^\s*\[(?:直播提示|对方说|动作)\][^\n]*\n?", "", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def build_slot_prompt() -> str:
    """场景槽位提示（时段/直播阶段等），追加到 system prompt 之后。

    TODO(prompt-tuning)：上游这里拼的是节目单感知的场景描述。
    本层先返回空串——保持 system prompt 干净，避免引入未经验证的注入文本。
    """
    return ""


def log_turn(*, user=None, lumi=None, emotion=None, rag_hits=None,
             slot_snapshot=None, turn_type=None):
    """回合日志（conversation.py 在每轮收尾时以全关键字参数调用）。"""
    from launcher.registry import RUNTIME
    entry = {
        "at": time.time(),
        "turn_type": turn_type,
        "user": user,
        "lumi": lumi,
        "emotion": emotion,
        "rag_hits": rag_hits,
        "slot_snapshot": slot_snapshot,
    }
    RUNTIME.record_turn(**entry)
    if emotion:
        print(f"     [情绪] {emotion}")
    return entry


# ── 工具执行 ────────────────────────────────────────────────────────────

def execute_fast_brain_tools(tool_results, origin: str = ""):
    """把快脑产出的 tool_call 落到对应游戏桥接。

    参数形状由 conversation.py 的调用点决定：``(tool_results, "chat_and_speak")``，
    其中 tool_results 是 ``[{"name": ..., "arguments": {...}}, ...]``。

    ⚠️ **重要边界（读代码确认的）**：恶魔轮盘/Wordle/汉兜这类"待决决策"并不走这里——
    `conversation` 会自己把 tool_call 回填进待决请求：

        conversation.py:705-712   game_request = bridge.get_pending_decision()
        conversation.py:991-999   game_request.result = {...}; result_event.set()
        conversation.py:1023-1024 if not game_request: ctx.execute_fast_brain_tools(...)

    所以本函数只服务"**没有**待决 game_request 时的工具调用"（例如未来新增的
    非决策类游戏工具、画画工具）。此处实现的是通用回填：找到正在等待的请求，
    把参数写进 `request.result` 并 set 事件；找不到就只记录不误伤。
    """
    if not tool_results:
        return 0
    from launcher.registry import RUNTIME
    n = 0
    for call in tool_results:
        name = call.get("name") if isinstance(call, dict) else getattr(call, "name", None)
        args = call.get("arguments") if isinstance(call, dict) else getattr(call, "arguments", None)
        print(f"     [工具] {name}({args})  ← {origin}")
        for game_name in ("buckshot", "wordle", "handle", "kr", "terraria"):
            bridge = RUNTIME.get_bridge(game_name)
            if bridge is None:
                continue
            getter = getattr(bridge, "get_pending_decision", None)
            if not callable(getter):
                continue
            try:
                req = getter()
            except Exception:
                req = None
            if req is None or getattr(req, "cancelled", False):
                continue
            try:
                req.result.clear()
                req.result.update(args or {})
                req.result.setdefault("_tool", name)
                req.result_event.set()
                n += 1
                break
            except Exception as e:
                print(f"     [工具] 回填游戏决策失败：{e}")
    return n


# ── 游戏段落开场白（P4 用；此处给可用的最小实现）─────────────────────────

def kr_build_anchor_msg() -> str:
    return "（王国保卫战新一波开始了）"


def terraria_build_anchor_msg() -> str:
    return "（泰拉瑞亚这边刚回到地面）"


# ── 上游未开源功能的显式占位 ─────────────────────────────────────────────

def capture_screen():
    """TODO(unopened)：视觉/截图未开源。返回 None = 本轮不带截图。"""
    return None


def extract_draw_subject(item):
    """TODO(unopened)：画画主题抽取依赖未开源的视觉链路。"""
    return None


def mark_drawing_started(subject):
    """TODO(unopened)：画画开始标记。"""
    return None


def on_draw_complete(result):
    """TODO(unopened)：画画完成回调。"""
    return None


def get_draw_stage_offer():
    """TODO(unopened)：返回 None = 不注入画画工具。"""
    return None


def detect_activity_switch(user_input=None, current_activity=None):
    """TODO(prompt-tuning)：活动切换识别（上游是一次 LLM 判断）。

    调用形状由 conversation.py:655-659 决定——它在后台线程里以
    `args=(user_input, current_activity)` 调用本函数，所以签名必须收两个参数。

    本层保守返回 None：不主动切换状态机，避免未经验证的自动换场
    （自动换场会牵动状态机 TRANSITIONING + 拉起游戏桥接，属于 P4）。
    """
    return None


# ── 打断监听 ────────────────────────────────────────────────────────────

class EnergyVAD:
    """极简能量 VAD（不需要额外模型依赖）。

    为什么不用 silero/webrtcvad：本层的目标是把「人一开口就打断 AI」
    跑通且零新依赖；能量阈值对麦克风+固定增益的场景够用。
    升级路径：换成 silero-vad，接口不变（见 MicPump 的调用点）。
    """

    def __init__(self, *, threshold: float = 0.02, hang_frames: int = 4):
        self.threshold = threshold
        self.hang_frames = hang_frames
        self._above = 0

    def is_speech(self, frame) -> bool:
        try:
            import numpy as np
            rms = float(np.sqrt(np.mean(np.square(frame.astype("float32")))))
        except Exception:
            rms = 0.0
        if rms >= self.threshold:
            self._above += 1
        else:
            self._above = 0
        return self._above >= self.hang_frames


def interrupt_monitor(vad_model):
    """兼容占位：lumi_tts.speak 在每次出声时会以线程方式调用它（args=(vad_model,)）。

    本层的设计是把「麦克风采集 + VAD 打断」收敛到**一个常驻线程**
    （launcher.inputs.MicPump），因为这里同时还要把音频推给 ASR——
    两条线程各开一个麦克风流会互相抢设备。

    因此这个回调只做一件事：记录一次"本轮已有打断监听"。
    """
    from launcher.registry import RUNTIME
    RUNTIME.record_turn(event="interrupt_monitor_attached", vad=type(vad_model).__name__)
    return None


def b64_of_bytes(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
