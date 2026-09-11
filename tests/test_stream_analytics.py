"""直播可观测性模块（stream_analytics）的测试。

既测单元行为（喂事件 → 看统计），也测与真实仲裁器的集成（跑一段真实
抢麦流程 → 报告数字正确）。
"""
import time
import unittest

from event_bus import EventBus
from speech_output_arbiter import (
    SpeechOutputArbiter,
    POLICY_QUEUE,
    POLICY_PRIORITY,
)
from stream_analytics import StreamAnalytics


class AnalyticsUnitTests(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()
        self.analytics = StreamAnalytics(
            event_bus=self.bus, active_speakers=["fames", "tou"]
        )

    def test_speech_starts_and_durations_are_counted(self):
        self.bus.publish("speech_output_started", {
            "output_id": "a1", "speaker": "fames", "source": "chat",
            "policy": POLICY_QUEUE, "priority": 0, "started_at": time.time() - 0.5,
        }, source="test")
        self.bus.publish("speech_output_done", {
            "output_id": "a1", "speaker": "fames", "source": "chat",
            "policy": POLICY_QUEUE, "priority": 0,
        }, source="test")

        snap = self.analytics.summary()
        self.assertEqual(snap["speakers"]["fames"]["starts"], 1)
        self.assertGreater(snap["speakers"]["fames"]["total_speech_s"], 0.4)
        self.assertEqual(snap["open_outputs"], 0)

    def test_unfinished_speech_is_reported_as_open(self):
        self.bus.publish("speech_output_started", {
            "output_id": "a2", "speaker": "tou", "source": "chat",
            "policy": POLICY_QUEUE, "priority": 0, "started_at": time.time(),
        }, source="test")

        snap = self.analytics.summary()
        self.assertEqual(snap["speakers"]["tou"]["starts"], 1)
        self.assertEqual(snap["open_outputs"], 1)

    def test_contention_counters(self):
        self.bus.publish("speech_output_queued", {"speaker": "tou"}, source="test")
        self.bus.publish("speech_output_dropped", {"speaker": "tou"}, source="test")
        self.bus.publish("speech_output_cooldown_blocked",
                         {"speaker": "fames", "remaining_s": 0.4}, source="test")

        snap = self.analytics.summary()
        self.assertEqual(snap["speakers"]["tou"]["queued"], 1)
        self.assertEqual(snap["speakers"]["tou"]["dropped"], 1)
        self.assertEqual(snap["speakers"]["fames"]["cooldown_blocked"], 1)

    def test_preemption_is_not_double_counted_as_interruption(self):
        """抢占会同时触发 preempted 和 cancelled 两个事件，只能算一次。"""
        self.bus.publish("speech_output_started", {
            "output_id": "b1", "speaker": "tou", "source": "chat",
            "policy": POLICY_QUEUE, "priority": 0, "started_at": time.time(),
        }, source="test")
        self.bus.publish("speech_output_preempted", {
            "speaker": "fames", "source": "super_chat", "priority": 10,
            "preempted_speaker": "tou", "preempted_output_id": "b1",
            "preempted_priority": 0,
        }, source="test")
        self.bus.publish("speech_output_cancelled", {
            "output_id": "b1", "speaker": "tou", "source": "chat",
            "reason": "preempted_by_super_chat",
        }, source="test")

        snap = self.analytics.summary()
        self.assertEqual(snap["speakers"]["fames"]["preempted"], 1)
        self.assertEqual(snap["speakers"]["tou"]["interrupted"], 0,
                         "抢占已单独计数，不应再算进被打断")
        self.assertEqual(snap["open_outputs"], 0)

    def test_genuine_interruption_is_counted(self):
        self.bus.publish("speech_output_cancelled", {
            "output_id": "c1", "speaker": "fames", "source": "chat",
            "reason": "interrupted_by_asr",
        }, source="test")
        snap = self.analytics.summary()
        self.assertEqual(snap["speakers"]["fames"]["interrupted"], 1)

    def test_viewer_input_counting(self):
        self.analytics.record_viewer_input("danmaku")
        self.analytics.record_viewer_input("danmaku", count=2)
        self.analytics.record_viewer_input("super_chat")

        snap = self.analytics.summary()
        self.assertEqual(snap["viewer_messages"], 4)
        self.assertEqual(snap["viewer_by_source"]["danmaku"], 3)
        self.assertEqual(snap["viewer_by_source"]["super_chat"], 1)

    def test_state_durations_are_recorded(self):
        self.bus.publish("state_changed",
                         {"old": "IDLE", "new": "CHATTING"}, source="test")
        self.bus.publish("state_changed",
                         {"old": "CHATTING", "new": "ENDING"}, source="test")

        snap = self.analytics.summary()
        self.assertIn("CHATTING", snap["state_durations"])

    def test_unknown_speaker_is_still_counted(self):
        self.bus.publish("speech_output_started", {
            "output_id": "d1", "speaker": "someone_new", "source": "chat",
            "policy": POLICY_QUEUE, "priority": 0, "started_at": time.time(),
        }, source="test")
        self.assertIn("someone_new", self.analytics.summary()["speakers"])

    def test_detach_stops_counting(self):
        self.analytics.detach()
        self.bus.publish("speech_output_queued", {"speaker": "tou"}, source="test")
        self.assertEqual(self.analytics.summary()["speakers"]["tou"]["queued"], 0)


class AnalyticsReportTests(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()
        self.analytics = StreamAnalytics(
            event_bus=self.bus, active_speakers=["fames", "tou"]
        )

    def test_report_has_all_sections(self):
        self.analytics.record_viewer_input("danmaku")
        self.bus.publish("speech_output_started", {
            "output_id": "e1", "speaker": "fames", "source": "chat",
            "policy": POLICY_QUEUE, "priority": 0, "started_at": time.time(),
        }, source="test")
        self.bus.publish("speech_output_queued", {"speaker": "tou"}, source="test")

        report = self.analytics.report()

        for section in ("直播复盘报告", "[发言分布]", "[抢麦冲突]",
                        "[观众消息]", "fames", "tou"):
            self.assertIn(section, report)

    def test_report_handles_empty_stream(self):
        report = self.analytics.report()
        self.assertIn("本场没有发言记录", report)


class AnalyticsArbiterIntegrationTests(unittest.TestCase):
    """跑一段真实的抢麦流程，验证报告数字与仲裁器行为一致。"""

    def test_real_flow_is_reflected_in_the_report(self):
        bus = EventBus()
        arbiter = SpeechOutputArbiter(event_bus=bus)
        analytics = StreamAnalytics(event_bus=bus,
                                    active_speakers=["fames", "tou"])
        analytics.record_viewer_input("danmaku")

        # 1) fames 正常说一句
        a = arbiter.request_start(speaker="fames", source="chat")
        arbiter.mark_done(a.output_id)
        # 2) tou 想说话但被排队
        b = arbiter.request_start(speaker="fames", source="chat", ignore_cooldown=True)
        queued = arbiter.request_start(speaker="tou", source="chat",
                                       policy=POLICY_QUEUE)
        self.assertIsNone(queued)
        # 3) 付费消息抢占 fames
        sc = arbiter.request_start(speaker="tou", source="super_chat",
                                   policy=POLICY_PRIORITY, priority=5)
        arbiter.mark_done(sc.output_id)
        arbiter.mark_done(b.output_id)

        snap = analytics.summary()
        self.assertEqual(snap["speakers"]["fames"]["starts"], 2)
        self.assertEqual(snap["speakers"]["tou"]["queued"], 1)
        self.assertEqual(snap["speakers"]["tou"]["preempted"], 1)
        self.assertEqual(snap["speakers"]["tou"]["starts"], 1)
        self.assertEqual(snap["viewer_messages"], 1)
        self.assertEqual(snap["open_outputs"], 0)
        self.assertIn("tou", analytics.report())


if __name__ == "__main__":
    unittest.main()
