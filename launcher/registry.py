"""可变运行时状态的唯一收口。

对应 ConversationContext 里那些「lazy-bound 回调」要读的东西：
上游让它们每次都去读 `lumi` 模块的全局变量，是因为 bridge 对象在启动后才赋值。
本层保留这种「延迟绑定」，但把状态收进一个显式对象，避免全局变量散落各处。

注意：这里**只放进程级单例状态**（当前游戏控制器、开关、桥接表、会话 id）。
**per-session 的角色状态不放在这里**——那属于 realtime_chat 的 SessionRuntime。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RuntimeRegistry:
    # 进程级开关（argparse 在启动时写一次，运行时只读）
    enable_drawing: bool = False
    enable_tts: bool = True
    enable_audio: bool = True

    # 会话标识
    session_id: str = ""
    started_at: float = 0.0

    # 游戏：当前段落由谁操控（"" = 没有游戏段落）
    current_game_controller: str = ""

    # 游戏桥接（启动后才赋值 → 所以 context 里用 lambda 惰性读）
    bridges: dict[str, Any] = field(default_factory=dict)      # {"kr": bridge, ...}
    game_ready: dict[str, bool] = field(default_factory=dict)

    # 运行时事件
    stop_event: threading.Event = field(default_factory=threading.Event)
    last_viewer_message_at: float = 0.0
    last_agent_speech_at: float = 0.0

    # 回合日志（复盘与调试用）
    turn_log: list = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ── 桥接 ────────────────────────────────────────────────────────────
    def set_bridge(self, name: str, bridge, *, ready: bool = True):
        with self._lock:
            self.bridges[name] = bridge
            self.game_ready[name] = bool(ready)

    def get_bridge(self, name: str):
        with self._lock:
            return self.bridges.get(name)

    def is_game_ready(self, name: str) -> bool:
        with self._lock:
            return bool(self.game_ready.get(name))

    # ── 活跃时间（proactive 判断用）─────────────────────────────────────
    def touch_viewer_message(self, now: float):
        with self._lock:
            self.last_viewer_message_at = now

    def touch_agent_speech(self, now: float):
        with self._lock:
            self.last_agent_speech_at = now

    def idle_seconds(self, now: float) -> float:
        with self._lock:
            latest = max(self.last_viewer_message_at, self.last_agent_speech_at)
        if latest <= 0:
            return now - (self.started_at or now)
        return max(0.0, now - latest)

    # ── 回合日志 ────────────────────────────────────────────────────────
    def record_turn(self, **entry):
        with self._lock:
            self.turn_log.append(entry)

    def turns(self) -> list:
        with self._lock:
            return list(self.turn_log)


RUNTIME = RuntimeRegistry()
