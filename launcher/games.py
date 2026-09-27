"""游戏桥接接线（P4 第一项：恶魔轮盘）。

**这里有一个重要发现，先写清楚**（否则很容易接错地方）：

恶魔轮盘的决策回填**不是**由 `launcher/helpers.execute_fast_brain_tools` 完成的，
而是由 `conversation.chat_and_speak` 自己完成：

    conversation.py:705-712   game_request = bridge.get_pending_decision()
    conversation.py:991-999   tool_results[0] → game_request.result = {...}; result_event.set()
    conversation.py:1023-1024 if not game_request: ctx.execute_fast_brain_tools(...)

也就是说：`execute_fast_brain_tools` **只在没有待决 game_request 时**才被调用
（泰拉瑞亚走它自己的分支，见 conversation.py:1010-1022）。

所以"接通游戏"要做的三件事是：
  1. **起桥接**并注册到 RUNTIME（`ctx.get_buckshot_bridge()` 是惰性回调，读的就是这里）；
  2. **把状态机切到 PLAYING_* **：桥接用 `bus` 构造时会订阅 `state_changed`，
     只有全局状态等于 `EXPECTED_GLOBAL_STATE`（"PLAYING_BUCKSHOT"）时
     `_activation_event` 才被 set，决策才允许发生（bridge.py:469-490）；
  3. **给待决决策让路**：主循环要在「有 pending decision」时立刻跑一轮对话，
     而不是等空闲超时（否则 20 秒的决策窗口会被白白耗掉）。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

from launcher.registry import RUNTIME


@dataclass
class GameSession:
    name: str
    bridge: object
    thread: threading.Thread = None
    mock: object = None
    extra: dict = field(default_factory=dict)

    def stop(self):
        if self.bridge is not None:
            try:
                self.bridge.stop()
            except Exception:
                pass
        if self.mock is not None:
            try:
                self.mock.stop()
            except Exception:
                pass


def start_game(*, cfg, bus, state, log_fn) -> dict:
    """按 cfg.game 拉起游戏段落。返回 {游戏名: GameSession}。"""
    if cfg.game in ("none", "", None):
        return {}
    if cfg.game != "buckshot":
        log_fn(f"[游戏] --game {cfg.game} 尚未接线（P4 后续项）。"
               f"各 bridge 的构造与事件协议见 games/*/bridge.py；"
               f"未接线时 conversation 的游戏分支会自动跳过，不会影响直播。")
        return {}
    return {"buckshot": _start_buckshot(cfg=cfg, bus=bus, state=state, log_fn=log_fn)}


def _start_buckshot(*, cfg, bus, state, log_fn) -> GameSession:
    from games.buckshot.bridge import BuckshotBridge, PORT, HOST

    mock = None
    if getattr(cfg, "mock_game", False):
        from launcher.mock_game import MockGodotServer
        mock = MockGodotServer(host=HOST, port=PORT, log_fn=log_fn).start()
        log_fn(f"[游戏·模拟端] 已在 {HOST}:{PORT} 起模拟游戏端（不需要真实游戏）")

    # controller_provider 必须每次现拉真值（bridge 刻意不缓存：缓存会与导演真值漂移）
    bridge = BuckshotBridge(bus=bus,
                            controller_provider=lambda: RUNTIME.current_game_controller)
    RUNTIME.current_game_controller = cfg.characters[0]
    RUNTIME.set_bridge("buckshot", bridge, ready=True)

    thread = threading.Thread(target=bridge.run, daemon=True, name="buckshot-bridge")
    thread.start()

    # 通知桥接：角色上下文已就绪（它会据此把操作者名字下发给游戏端）
    bus.publish("game_role_context_changed", {"game_id": "buckshot_roulette"},
                source="launcher")
    log_fn(f"[游戏] 恶魔轮盘桥接已启动；操作者 = {RUNTIME.current_game_controller}"
           f"（等待状态机进入 PLAYING_BUCKSHOT 后才会请求决策）")
    return GameSession(name="buckshot", bridge=bridge, thread=thread, mock=mock)


def has_pending_decision() -> tuple:
    """是否有游戏在等待快脑决策。返回 (游戏名, request) 或 (None, None)。

    主循环用它给决策让路——否则桥接要等满 20-35 秒超时才走兜底。
    """
    for name in ("buckshot", "wordle", "handle"):
        br = RUNTIME.get_bridge(name)
        if br is None:
            continue
        try:
            req = br.get_pending_decision()
        except Exception:
            req = None
        if req is not None and not getattr(req, "cancelled", False):
            return name, req
    return None, None


def stop_games(sessions: dict, log_fn) -> None:
    for name, sess in (sessions or {}).items():
        try:
            sess.stop()
            log_fn(f"[游戏] {name} 桥接已停止")
        except Exception as e:
            log_fn(f"  ! 停游戏桥接 {name} 失败：{e}")
