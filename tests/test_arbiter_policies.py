"""新增抢麦策略的测试：优先级抢占（POLICY_PRIORITY）与连麦冷却。

覆盖 speech_output_arbiter 上新增的两条策略，以及默认配置的向后兼容性。
"""
import time
import unittest

from event_bus import EventBus
from speech_output_arbiter import (
    SpeechOutputArbiter,
    POLICY_QUEUE,
    POLICY_DROP,
    POLICY_PRIORITY,
)


class PriorityPreemptionTests(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()
        self.events = []
        for et in ("speech_output_preempted", "speech_output_cancelled",
                   "speech_output_queued", "speech_output_started"):
            self.bus.subscribe(et, lambda e: self.events.append(e))
        self.arbiter = SpeechOutputArbiter(event_bus=self.bus)

    def _types(self):
        return [e.event_type for e in self.events]

    def test_higher_priority_takes_the_floor(self):
        low = self.arbiter.request_start(speaker="tou", source="chat")
        self.assertIsNotNone(low)

        high = self.arbiter.request_start(
            speaker="fames", source="super_chat", policy=POLICY_PRIORITY, priority=10
        )

        self.assertIsNotNone(high, "更高优先级应当拿到发言权")
        self.assertEqual(high.priority, 10)
        self.assertIs(self.arbiter.current(), high)
        # 被抢占的一方必须被标记为已取消，TTS 才能据此停播
        self.assertTrue(self.arbiter.is_cancelled(low.output_id))
        self.assertFalse(self.arbiter.is_current(low.output_id))
        self.assertIn("speech_output_preempted", self._types())

    def test_equal_or_lower_priority_queues_instead_of_preempting(self):
        low = self.arbiter.request_start(speaker="tou", source="chat")

        same = self.arbiter.request_start(
            speaker="fames", source="chat", policy=POLICY_PRIORITY, priority=0
        )

        self.assertIsNone(same, "优先级不高于当前持有者时应当排队")
        self.assertIs(self.arbiter.current(), low, "当前持有者不应被打断")
        self.assertFalse(self.arbiter.is_cancelled(low.output_id))
        self.assertIn("speech_output_queued", self._types())
        self.assertNotIn("speech_output_preempted", self._types())

    def test_idle_floor_is_taken_regardless_of_priority(self):
        out = self.arbiter.request_start(
            speaker="fames", source="super_chat", policy=POLICY_PRIORITY, priority=99
        )
        self.assertIsNotNone(out)
        self.assertEqual(out.priority, 99)

    def test_default_priority_is_zero(self):
        out = self.arbiter.request_start(speaker="fames", source="chat")
        self.assertEqual(out.priority, 0)


class SpeakerCooldownTests(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()
        self.events = []
        self.bus.subscribe("speech_output_cooldown_blocked",
                           lambda e: self.events.append(e))
        self.arbiter = SpeechOutputArbiter(event_bus=self.bus)

    def test_same_speaker_is_blocked_right_after_releasing(self):
        self.arbiter.set_cooldown(0.5)
        first = self.arbiter.request_start(speaker="fames", source="chat")
        self.arbiter.mark_done(first.output_id)

        again = self.arbiter.request_start(speaker="fames", source="chat")

        self.assertIsNone(again, "刚交还发言权的角色不应立刻再开口")
        self.assertGreater(self.arbiter.cooldown_remaining("fames"), 0.0)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0].data["speaker"], "fames")

    def test_other_speaker_is_not_blocked(self):
        self.arbiter.set_cooldown(0.5)
        first = self.arbiter.request_start(speaker="fames", source="chat")
        self.arbiter.mark_done(first.output_id)

        other = self.arbiter.request_start(speaker="tou", source="chat")

        self.assertIsNotNone(other, "冷却只约束触发它的那个角色")
        self.assertEqual(self.arbiter.cooldown_remaining("tou"), 0.0)

    def test_cooldown_expires(self):
        self.arbiter.set_cooldown(0.05)
        first = self.arbiter.request_start(speaker="fames", source="chat")
        self.arbiter.mark_done(first.output_id)
        time.sleep(0.08)

        after = self.arbiter.request_start(speaker="fames", source="chat")

        self.assertIsNotNone(after, "冷却到期后应恢复开口")

    def test_ignore_cooldown_is_an_escape_hatch(self):
        self.arbiter.set_cooldown(5.0)
        first = self.arbiter.request_start(speaker="fames", source="chat")
        self.arbiter.mark_done(first.output_id)

        forced = self.arbiter.request_start(
            speaker="fames", source="chat", ignore_cooldown=True
        )

        self.assertIsNotNone(forced, "被 @ 点名时应当能绕过冷却")

    def test_cooldown_disabled_by_default(self):
        self.assertEqual(self.arbiter.cooldown_s(), 0.0)
        first = self.arbiter.request_start(speaker="fames", source="chat")
        self.arbiter.mark_done(first.output_id)
        again = self.arbiter.request_start(speaker="fames", source="chat")
        self.assertIsNotNone(again, "默认关闭冷却，行为与改动前一致")

    def test_setting_negative_cooldown_is_clamped(self):
        self.arbiter.set_cooldown(-3)
        self.assertEqual(self.arbiter.cooldown_s(), 0.0)


class BackwardCompatibilityTests(unittest.TestCase):
    """旧的三种策略必须保持原样，不能被新策略改坏。"""

    def setUp(self):
        self.arbiter = SpeechOutputArbiter()

    def test_queue_policy_still_queues(self):
        held = self.arbiter.request_start(speaker="fames", source="chat")
        blocked = self.arbiter.request_start(speaker="tou", source="chat",
                                             policy=POLICY_QUEUE)
        self.assertIsNone(blocked)
        self.assertEqual(self.arbiter.queued_count(), 1)
        self.assertIs(self.arbiter.current(), held)

    def test_drop_policy_still_drops(self):
        held = self.arbiter.request_start(speaker="fames", source="chat")
        dropped = self.arbiter.request_start(speaker="tou", source="chat",
                                             policy=POLICY_DROP)
        self.assertIsNone(dropped)
        self.assertEqual(self.arbiter.queued_count(), 0)
        self.assertIs(self.arbiter.current(), held)


if __name__ == "__main__":
    unittest.main()
