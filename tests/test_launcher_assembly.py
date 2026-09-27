"""启动器装配契约测试。

**不需要任何 API key、声卡、网络**——测的是"零件有没有接对"，
而不是"模型答得好不好"。CI（Python 3.12）与本机 lumi_nox 环境（3.10）都能跑。

这些用例专门守住三类"静默失效"的装配错误：
  1. 仲裁器没接总线 → 抢麦事件不上总线 → 复盘报告永远是 0（用例 2）；
  2. 冷却没显式打开 → 新策略永远不生效（用例 3）；
  3. context_slot 缺 activity_type → 每轮 KeyError（用例 4）。
"""
from __future__ import annotations

import os
import unittest

# ⚠️ 必须排在导入 conversation/fast_brain 之前：fast_brain 在**导入期**就构造
# OpenAI 客户端，新版 SDK 缺 key 会直接抛 OpenAIError；且 .env 里常见的
# `ARK_API_KEY_FAST=`（键存在、值为空）也会让 SDK 报缺凭证，所以不能用 setdefault。
for _k in ("ARK_API_KEY_FAST", "DASHSCOPE_API_KEY",
           "VOLC_DIALOG_APP_ID", "VOLC_DIALOG_ACCESS_KEY"):
    if not os.environ.get(_k):
        os.environ[_k] = "test-placeholder"

from launcher.assemble import build_engine                      # noqa: E402
from launcher.config import LauncherConfig                      # noqa: E402


def make_cfg(**kw) -> LauncherConfig:
    base = dict(arch="text", characters=["fames", "tou"], dry_run=True,
                enable_audio=False, enable_tts=False, no_memory=True,
                danmaku="none", db_path="memory/test_launcher.db", cooldown=0.0)
    base.update(kw)
    return LauncherConfig(**base)


class AssemblyTests(unittest.TestCase):
    def setUp(self):
        # 仲裁器是**模块级单例**，它的 `_last_release_at` 里记录着各角色上次交还发言权的
        # 时刻（只在冷却开启时被读，且不会自动过期）。用例之间会互相影响：
        # 上一个用例刚 mark_done("fames")，下一个用例一开冷却就会把 fames 判成"冷却中"。
        # 所以这里显式把单例状态复位——顺带说明一个真实性质：
        # **同一进程内重启直播时，冷却状态是跨场次保留的**（见 docs/LAUNCHER.md 已知限制）。
        import speech_output_arbiter as soa
        soa.arbiter.set_cooldown(0.0)
        soa.arbiter._last_release_at.clear()
        soa.arbiter._current = None

    def tearDown(self):
        import speech_output_arbiter as soa
        soa.arbiter.set_cooldown(0.0)
        soa.arbiter._last_release_at.clear()
        soa.arbiter._current = None

    # ── 1. 上下文装配完整性 ────────────────────────────────────────────
    def test_context_is_fully_assembled(self):
        eng = build_engine(make_cfg(), log_fn=lambda *_: None)
        ctx = eng.context
        self.assertEqual(set(ctx.active_speakers), {"fames", "tou"})
        self.assertEqual(set(ctx.fast_brains), set(ctx.active_speakers))
        self.assertIs(ctx.scheduler, eng.scheduler)
        # 关键回调都必须被填上（任一为 None 都会在运行期炸）
        for name in ("log_event", "parse_emotion", "strip_stage_directions",
                     "build_slot_prompt", "log_turn", "execute_fast_brain_tools",
                     "interrupt_monitor", "touch_last_message"):
            self.assertIsNotNone(getattr(ctx, name), f"{name} 未装配")
        # lazy-bound 游戏桥接：未起桥接时应返回 None 而不是抛异常
        self.assertIsNone(ctx.get_kr_bridge())
        self.assertFalse(ctx.get_kr_game_ready())

    # ── 2. 仲裁器 ↔ 事件总线 ↔ 可观测性（最关键的一条）────────────────
    def test_arbiter_is_wired_to_bus_and_analytics(self):
        eng = build_engine(make_cfg(), log_fn=lambda *_: None)
        out = eng.arbiter.request_start(speaker="fames", source="chat")
        self.assertIsNotNone(out, "空闲时应当拿到发言权")
        eng.arbiter.mark_done(out.output_id)

        snap = eng.analytics.summary()
        self.assertEqual(snap["speakers"]["fames"]["starts"], 1,
                         "仲裁器事件没有上总线 → 报告全 0（检查 arbiter.configure）")
        self.assertEqual(snap["open_outputs"], 0)

    # ── 3. 冷却策略必须被显式打开才生效 ───────────────────────────────
    def test_cooldown_is_applied_and_per_speaker(self):
        eng = build_engine(make_cfg(cooldown=5.0), log_fn=lambda *_: None)
        self.assertEqual(eng.arbiter.cooldown_s(), 5.0,
                         "冷却没被 set_cooldown 打开 → 新策略永远不生效")
        first = eng.arbiter.request_start(speaker="fames", source="chat")
        eng.arbiter.mark_done(first.output_id)
        again = eng.arbiter.request_start(speaker="fames", source="chat")
        self.assertIsNone(again, "刚交还发言权的角色不应立刻再开口")
        other = eng.arbiter.request_start(speaker="tou", source="chat")
        self.assertIsNotNone(other, "冷却只约束触发它的那个角色")

        # ⚠️ 逃生通道只绕过**冷却**，不绕过**话筒互斥**：
        # tou 此刻还占着话筒，所以 fames 带 ignore_cooldown 也只会被排队（返回 None）。
        # 这正好说明两件事：① 冷却与互斥是正交的两道闸；② 被排队的请求在本仓库里
        # 没有消费者（排队语义的消费端在未发布的主程序里）——所以付费互动走"抢占"而不是"排队"。
        queued = eng.arbiter.request_start(speaker="fames", source="chat",
                                           ignore_cooldown=True)
        self.assertIsNone(queued, "话筒被占用时，绕过冷却也只能排队")

        eng.arbiter.mark_done(other.output_id)      # 先把话筒交还
        forced = eng.arbiter.request_start(speaker="fames", source="chat",
                                           ignore_cooldown=True)
        self.assertIsNotNone(forced, "话筒空闲时，被点名应能绕过冷却立刻开口")

    # ── 4. 干跑实测踩到的坑：context_slot 必须预置 activity_type ────────
    def test_context_slot_has_activity_type(self):
        eng = build_engine(make_cfg(), log_fn=lambda *_: None)
        # conversation.py:654 用下标访问，缺键会在每一轮直接 KeyError
        self.assertIn("activity_type", eng.context.context_slot)

    # ── 5. 单角色退化 ─────────────────────────────────────────────────
    def test_single_character_degrades(self):
        eng = build_engine(make_cfg(characters=["fames"]), log_fn=lambda *_: None)
        sch = eng.scheduler
        self.assertEqual(sch.next_speaker, "fames")
        sch.advance()
        self.assertEqual(sch.next_speaker, "fames", "单角色下轮换不应越界")
        self.assertIsNone(eng.context.get_kr_bridge())

    # ── 6. 输入格式必须能被调度器正确路由 ─────────────────────────────
    def test_viewer_item_format_routes_correctly(self):
        from launcher.inputs import ViewerItem
        eng = build_engine(make_cfg(), log_fn=lambda *_: None)
        sch = eng.scheduler
        item = ViewerItem(body="tou 讲个冷笑话", source="danmaku", label="rin", uid=1002)
        text = item.as_scheduler_text()
        self.assertEqual(text, "弹幕：rin：tou 讲个冷笑话")
        # 前缀里的 "rin" 不应干扰判名；正文里的 tou 应当被识别
        self.assertEqual(sch.detect_addressed_speaker(text), "tou")
        # 同时 @ 两个人 → 歧义 → 交给轮换（返回 None）
        both = ViewerItem(body="fames 和 tou 都说说", label="mona")
        self.assertIsNone(sch.detect_addressed_speaker(both.as_scheduler_text()))

    # ── 7. 记忆身份键 ─────────────────────────────────────────────────
    def test_memory_identity_keys(self):
        from launcher.inputs import ViewerItem
        self.assertEqual(ViewerItem(body="hi", label="mona", uid=1001).identity_key(),
                         "bili:1001")
        self.assertEqual(ViewerItem(body="hi", label="mona").identity_key(),
                         "legacy:mona")

    # ── 8. 复盘报告：时长极小时的口径回退（干跑实测发现的显示 bug）─────
    def test_report_share_falls_back_to_count(self):
        from event_bus import EventBus
        from stream_analytics import StreamAnalytics
        bus = EventBus()
        an = StreamAnalytics(event_bus=bus, active_speakers=["fames", "tou"])
        for i, sp in enumerate(("fames", "tou")):
            oid = f"o{i}"
            bus.publish("speech_output_started",
                        {"output_id": oid, "speaker": sp, "source": "chat"},
                        source="test")
            bus.publish("speech_output_done",
                        {"output_id": oid, "speaker": sp, "source": "chat"},
                        source="test")
        report = an.report()
        self.assertIn("占比按发言次数计算", report,
                      "零时长场景下按时长算占比会退化成 100%/0%")
        self.assertIn("占比 50%", report)


class WiringCoverageTests(unittest.TestCase):
    """把"哪些孤儿函数已经被本层接上"写成可执行清单，防止回退。"""

    def test_conversation_context_fields_are_all_supplied(self):
        from dataclasses import MISSING, fields
        eng = build_engine(make_cfg(), log_fn=lambda *_: None)
        ctx = eng.context
        from launcher.context import OPTIONAL_NONE
        unfilled = [
            f.name for f in fields(ctx)
            if f.default is MISSING and f.default_factory is MISSING
            and f.name not in OPTIONAL_NONE and getattr(ctx, f.name, None) is None
        ]
        self.assertEqual(unfilled, [], f"这些字段没被填：{unfilled}")


if __name__ == "__main__":
    unittest.main()
