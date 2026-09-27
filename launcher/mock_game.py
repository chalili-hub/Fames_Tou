"""模拟游戏端（TCP 服务器），用于**在没有真实游戏的情况下验证桥接接线**。

它按 `games/buckshot/bridge.py` 的协议说话（换行分隔 JSON）：

  桥接 → 游戏：{"action": "set_auto_mode" | "set_player_name" | "shoot" | "use_item", ...}
  游戏 → 桥接：{"type": "connected" | "game_state" | "game_event" | "action_executed", ...}

它故意构造一个**确定性引擎无法决策**的局面：
    混合弹匣（4 发 = 2 实 + 2 空）+ 玩家无道具 + 无情报
    → `DeterministicEngine.decide()` 走到最后 `return None`（bridge.py:322）
    → 桥接把决策投递给快脑 → 才能验证「快脑决策 → 回填 → 执行」整条链路。

⚠️ 这是**开发/验证工具**，不是游戏的一部分：真实游戏端是 Godot BridgeMod。
"""
from __future__ import annotations

import json
import socket
import threading
import time

# 让确定性引擎放弃决策的局面（bridge.py:268-322 的判定链）
ABSTAIN_STATE = {
    "type": "game_state",
    "phase": "player_turn",
    "health_player": 3,
    "health_opponent": 3,
    "max_health": 4,
    "shells_sequence": ["live", "blank", "live", "blank"],
    "shells_remaining": 4,
    "shells_live_total": 2,
    "shells_blank_total": 2,
    "current_shell": "unknown",
    "shotgun_damage": 1,
    "barrel_sawed_off": False,
    "dealer_cuffed": False,
    "player_cuffed": False,
    "player_items": [],          # 无道具 → 没有放大镜可用 → 引擎只能交给 LLM
    "dealer_items": [],
    "round": 1,
    "batch": 1,
}


class MockGodotServer:
    def __init__(self, *, host: str = "127.0.0.1", port: int = 9876, log_fn=print):
        self.host, self.port = host, port
        self._log = log_fn
        self._srv = None
        self._conn = None
        self._stop = threading.Event()
        self._thread = None
        self.received: list = []            # 收到的所有命令
        self.received_event = threading.Event()
        self._responded = False
        self._started = False
        self._last_state_sent = 0.0

    # ── 生命周期 ──────────────────────────────────────────────────────
    def start(self):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._srv.bind((self.host, self.port))
        except OSError as e:
            self._log(f"[模拟游戏端] 端口 {self.port} 绑定失败：{e}"
                      f"（可能已有真实游戏或另一个模拟端在监听）")
            raise
        self._srv.listen(1)
        self._srv.settimeout(0.2)
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="mock-godot")
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        for s in (self._conn, self._srv):
            try:
                if s is not None:
                    s.close()
            except Exception:
                pass

    def wait_for_command(self, actions=("shoot", "use_item"), timeout: float = 30.0):
        """等桥接下发的决策命令（用于测试断言）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for cmd in list(self.received):
                if cmd.get("action") in actions:
                    return cmd
            time.sleep(0.05)
        return None

    # ── 内部 ──────────────────────────────────────────────────────────
    def _send(self, obj: dict):
        if self._conn is None:
            return
        try:
            self._conn.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        except Exception as e:
            self._log(f"[模拟游戏端] 发送失败：{e}")

    def _serve(self):
        self._log(f"[模拟游戏端] 监听 {self.host}:{self.port}")
        while not self._stop.is_set():
            try:
                conn, addr = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._log(f"[模拟游戏端] 桥接已连接：{addr}")
            self._conn = conn
            self._conn.settimeout(0.2)
            buf = ""
            while not self._stop.is_set():
                try:
                    data = conn.recv(4096)
                except socket.timeout:
                    data = b""
                except OSError:
                    break
                if data == b"" and not self._stop.is_set():
                    # 超时分支：真实游戏是**持续推状态**的，这里也周期重发，
                    # 否则会踩一个时序竞态——桥接刚连上时状态机可能还没切到
                    # PLAYING_BUCKSHOT，此刻下发的局面会被门控丢掉且不再重发。
                    if self._started and not self._responded and \
                            time.time() - self._last_state_sent > 1.0:
                        self._send(ABSTAIN_STATE)
                        self._last_state_sent = time.time()
                    continue
                if not data:
                    break
                buf += data.decode("utf-8", errors="replace")
                while "\n" in buf:
                    idx = buf.index("\n")
                    line = buf[:idx].strip()
                    buf = buf[idx + 1:]
                    if not line:
                        continue
                    try:
                        cmd = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self._on_command(cmd)
            try:
                conn.close()          # 显式关闭，避免测试里刷 unclosed socket 告警
            except Exception:
                pass
            self._conn = None
            self._log("[模拟游戏端] 连接已断开")
        self._log("[模拟游戏端] 已停止")

    def _on_command(self, cmd: dict):
        self.received.append(cmd)
        action = cmd.get("action", "")
        self._log(f"[模拟游戏端] <<< 收到命令: {json.dumps(cmd, ensure_ascii=False)}")
        self.received_event.set()

        if action == "set_auto_mode":
            self._send({"type": "connected", "message": "MockGodot ready"})
            self._send(ABSTAIN_STATE)
            self._last_state_sent = time.time()
            self._started = True
            self._log("[模拟游戏端] 已下发玩家回合局面（混合弹匣 + 无道具 → 应交给快脑）")
            return

        if action in ("shoot", "use_item") and not self._responded:
            self._responded = True
            self._send({"type": "action_executed", "action": action,
                        "item": cmd.get("item", ""), "target": cmd.get("target", "")})
            self._send({"type": "game_event", "event": "player_shot_dealer_blank",
                        "damage": 0})
            # 切到庄家回合，避免同一局面被反复决策
            dealer_turn = dict(ABSTAIN_STATE, phase="dealer_turn", shells_remaining=3,
                               shells_sequence=["blank", "live", "blank"])
            self._send(dealer_turn)
            self._log("[模拟游戏端] 决策已收到并执行，局面切到庄家回合")
