"""整场直播运行架构的单一真相源。

开播时由 lumi.py 从 --chat-arch 设定一次，运行中不再改变。所有"此刻该不该
走端到端"的判断都查这里，不再各自看状态机。

架构取值：
- ``text``    : 独立链路（ASR + LLM + 独立 TTS/CosyVoice）
- ``realtime``: 旧版端到端会话（doubao SC2.0 二进制协议，realtime_chat.py）
- ``duplex``  : 新版端到端实时语音（JSON 事件协议，launcher/duplex_client.py）

> ``duplex`` 由启动器引入：它同样是"独立发声"（不借 realtime_chat），
> 但走 launcher 注入的发声器工厂（见 lumi_tts.set_emitter_factory）。
"""

_VALID = ("text", "realtime", "duplex")
_architecture = "text"  # 改造后默认：文本架构


def set_architecture(mode: str) -> None:
    if mode not in _VALID:
        raise ValueError(f"未知运行架构: {mode}，可选: {_VALID}")
    global _architecture
    _architecture = mode


def get_architecture() -> str:
    return _architecture


def reset() -> None:
    """复位到默认（仅测试用）。"""
    global _architecture
    _architecture = "text"


def is_realtime_active(state_name: str) -> bool:
    """端到端聊天链路此刻是否生效：端到端架构 且 处于聊天状态。"""
    return _architecture == "realtime" and state_name == "CHATTING"


def use_independent_tts() -> bool:
    """发声是否走独立输出（而非借 realtime_chat 的 say_streaming）。

    ``text`` 架构走 CosyVoice；``duplex`` 架构走 duplex 适配器——
    两者都不经过 realtime_chat，所以都算"独立"。
    """
    return _architecture in ("text", "duplex")


def use_duplex() -> bool:
    """是否使用新版端到端实时语音（JSON 事件协议）。"""
    return _architecture == "duplex"
