"""游戏桥接接线的回归测试（恶魔轮盘）。

不需要真实游戏、不需要 LLM、不需要网络：
用 `launcher.mock_game.MockGodotServer` 假装成 Godot 端，验证

    引擎放弃决策 → 桥接投递待决请求 → 回填中文动作 → 桥接校验并映射成命令 → 下发到游戏端

上游的映射逻辑（`command_from_chinese_action`）与校验兜底（`_validate_decision`）
都在桥接里，所以这条链路能把"快脑给的中文动作"一路验证到"游戏端收到的命令"。
"""
from __future__ import annotations

import socket
import threading
import time
import unittest


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class BuckshotWiringTests(unittest.TestCase):
    def setUp(self):
        from games.buckshot import bridge as B
        self.B = B
        self.port = _free_port()
        self._orig_port = B.PORT
        B.PORT = self.port           # 桥接在 connect() 时读模块级 PORT

        from launcher.mock_game import MockGodotServer
        self.mock = MockGodotServer(host="127.0.0.1", port=self.port,
                                    log_fn=lambda *_: None).start()
        self.bridge = B.BuckshotBridge()      # 不传 bus → 决策门控恒开（测试聚焦协议）
        self.thread = threading.Thread(target=self.bridge.run, daemon=True)
        self.thread.start()

    def tearDown(self):
        try:
            self.bridge.stop()
        except Exception:
            pass
        try:
            self.mock.stop()
        except Exception:
            pass
        self.B.PORT = self._orig_port

    def _wait_pending(self, timeout=8.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            req = self.bridge.get_pending_decision()
            if req is not None:
                return req
            time.sleep(0.05)
        return None

    def test_bridge_connects_and_sends_identity(self):
        """桥接连上后应下发自动模式与操作者名字（跨进程协议的前两步）。"""
        deadline = time.time() + 8
        while time.time() < deadline:
            actions = [c.get("action") for c in self.mock.received]
            if "set_auto_mode" in actions and "set_player_name" in actions:
                break
            time.sleep(0.05)
        actions = [c.get("action") for c in self.mock.received]
        self.assertIn("set_auto_mode", actions, f"未收到 set_auto_mode，实际={actions}")
        self.assertIn("set_player_name", actions, f"未收到 set_player_name，实际={actions}")
        name_cmd = next(c for c in self.mock.received if c.get("action") == "set_player_name")
        # controller_provider 为空时桥接用 "fames" 兜底（bridge.py:551-559），
        # 避免游戏端拿到空字符串签字
        self.assertTrue(name_cmd.get("name"), "set_player_name 不能是空名字")

    def test_decision_roundtrip_maps_chinese_action_to_command(self):
        """核心链路：待决请求 → 回填中文动作 → 桥接映射 → 游戏端收到 shoot/dealer。"""
        req = self._wait_pending()
        self.assertIsNotNone(req, "引擎应把'混合弹匣+无道具'的局面交给快脑决策")

        # 可选动作里必须同时有"射击庄家/射击自己"，且没给道具时不应出现道具动作
        enum = req.tools[0]["function"]["parameters"]["properties"]["动作"]["enum"]
        self.assertIn("射击庄家", enum)
        self.assertIn("射击自己", enum)
        self.assertFalse([a for a in enum if a.startswith("使用")],
                         f"玩家无道具时不应出现道具动作：{enum}")

        # 模拟 conversation 的回填（conversation.py:991-999 就是这个形状）
        req.result.update({"action": "choose_buckshot_action", "动作": "射击庄家"})
        req.result_event.set()

        cmd = self.mock.wait_for_command(actions=("shoot", "use_item"), timeout=10)
        self.assertIsNotNone(cmd, "游戏端应收到决策命令")
        self.assertEqual(cmd["action"], "shoot")
        self.assertEqual(cmd["target"], "dealer",
                         "中文动作'射击庄家'必须被桥接映射成 target=dealer")
        self.assertEqual(cmd.get("_layer"), "lumi_fast_brain",
                         "决策来源应被标记为快脑，便于复盘追踪")

    def test_invalid_action_falls_back_to_deterministic_strategy(self):
        """快脑给无效动作时，桥接必须自己兜底（不能把非法命令发给游戏端）。"""
        req = self._wait_pending()
        self.assertIsNotNone(req)
        req.result.update({"action": "choose_buckshot_action", "动作": "跳个舞"})
        req.result_event.set()

        cmd = self.mock.wait_for_command(actions=("shoot", "use_item"), timeout=10)
        self.assertIsNotNone(cmd, "兜底策略也应该下发一个合法命令")
        self.assertIn(cmd["action"], ("shoot", "use_item"))
        self.assertEqual(cmd.get("_layer"), "fallback_invalid_action",
                         "非法动作应走 fallback_invalid_action 分支")


if __name__ == "__main__":
    unittest.main()
