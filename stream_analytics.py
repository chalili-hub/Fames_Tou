"""直播可观测性模块 — 订阅事件总线，复盘一场直播的发言分布与抢麦冲突。

设计意图
--------
这个模块是**纯事件消费者**：它不认识调度器、不认识 TTS，只订阅事件总线上的
事件，自己维护统计。任何模块都不需要为了"被统计"而改一行代码——这正是
``event_bus.py`` 把各层解耦之后应该拿到的红利。

它回答的是直播运营最关心的几个问题：

* **发言分布**：每个角色说了多少次、累计说了多久、占比多少？有没有人霸场？
* **抢麦冲突**：多少次请求被排队 / 被丢弃 / 被打断 / 被优先级抢占 / 被冷却拦截？
  （后两项来自 ``speech_output_arbiter`` 的优先级抢占与连麦冷却策略）
* **弹幕吞吐**：这一场收到多少条观众消息？
* **状态时间线**：时间都花在了聊天还是打游戏上？

``main.py`` 的演示会在结束时打印这份报告；生产环境里可以把它挂到真实的
直播进程上，下播后直接产出复盘数据。

订阅的事件
----------
``speech_output_started`` / ``speech_output_done`` / ``speech_output_queued`` /
``speech_output_dropped`` / ``speech_output_cancelled`` /
``speech_output_preempted`` / ``speech_output_cooldown_blocked`` / ``state_changed``
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class SpeakerStats:
    """单个角色的发言统计。"""
    starts: int = 0
    total_speech_s: float = 0.0
    queued: int = 0
    dropped: int = 0
    interrupted: int = 0
    preempted: int = 0
    cooldown_blocked: int = 0


class StreamAnalytics:
    """直播统计收集器（线程安全，零依赖）。

    用法::

        analytics = StreamAnalytics(event_bus=bus, active_speakers=["fames", "tou"])
        ...
        print(analytics.report())
    """

    STATE_EVENT = "state_changed"
    SUBSCRIBED_EVENTS = (
        "speech_output_started",
        "speech_output_done",
        "speech_output_queued",
        "speech_output_dropped",
        "speech_output_cancelled",
        "speech_output_preempted",
        "speech_output_cooldown_blocked",
        STATE_EVENT,
    )

    def __init__(self, *, event_bus=None, active_speakers=None, log_fn=None):
        self._lock = threading.RLock()
        self._active_speakers = list(active_speakers or [])
        self._speakers: dict[str, SpeakerStats] = {
            name: SpeakerStats() for name in self._active_speakers
        }
        # output_id -> (speaker, started_at)：正在进行的发言
        self._open: dict[str, tuple[str, float]] = {}
        # 已被抢占的 output_id：抢占会同时触发 cancelled，靠这份集合去重
        self._preempted_ids: set[str] = set()
        self._viewer_messages = 0
        self._viewer_by_source: dict[str, int] = {}
        self._state_durations: dict[str, float] = {}
        self._state_entered_at: float | None = None
        self._current_state: str = ""
        self._started_at = time.time()
        self._last_report_at: float | None = None
        self._log_fn = log_fn or (lambda _msg: None)
        self._event_bus = None
        # 绑定方法每次属性访问都会新建对象，而 EventBus.unsubscribe 用的是 `is not`
        # 身份比较——必须先存成同一个对象，否则解绑会静默失败。
        self._handler = self._on_event
        if event_bus is not None:
            self.attach(event_bus)

    # ── 装配 ────────────────────────────────────────────────────────────
    def attach(self, event_bus):
        """订阅事件总线。重复调用会先解绑，避免重复计数。"""
        with self._lock:
            self.detach()
            self._event_bus = event_bus
            for event_type in self.SUBSCRIBED_EVENTS:
                event_bus.subscribe(event_type, self._handler)

    def detach(self):
        with self._lock:
            bus, self._event_bus = self._event_bus, None
            if bus is None:
                return
            for event_type in self.SUBSCRIBED_EVENTS:
                try:
                    bus.unsubscribe(event_type, self._handler)
                except Exception as exc:  # 解绑失败不该影响主流程
                    self._log_fn(f"[analytics] unsubscribe {event_type} failed: {exc}")

    # ── 手动埋点（事件总线之外的数据）──────────────────────────────────
    def record_viewer_input(self, source: str = "danmaku", count: int = 1):
        """记录收到观众消息。

        调度器的入队目前不走事件总线，所以由调用方显式埋点；如果以后
        ``speaker_scheduler`` 也发布 ``viewer_input_enqueued`` 事件，
        改成订阅即可，本方法可以保留给测试和离线分析用。
        """
        with self._lock:
            self._viewer_messages += int(count)
            key = source or "unknown"
            self._viewer_by_source[key] = self._viewer_by_source.get(key, 0) + int(count)

    # ── 事件处理 ────────────────────────────────────────────────────────
    def _stats(self, speaker: str) -> SpeakerStats:
        """取（必要时新建）某个角色的统计。未知角色也计数，不丢数据。"""
        name = speaker or "未知"
        if name not in self._speakers:
            self._speakers[name] = SpeakerStats()
        return self._speakers[name]

    def _on_event(self, event):
        etype = getattr(event, "event_type", "")
        data = getattr(event, "data", {}) or {}
        now = getattr(event, "timestamp", None) or time.time()
        with self._lock:
            if etype == "speech_output_started":
                speaker = data.get("speaker", "")
                self._stats(speaker).starts += 1
                output_id = data.get("output_id")
                if output_id:
                    self._open[output_id] = (speaker, data.get("started_at") or now)
            elif etype in ("speech_output_done", "speech_output_cancelled"):
                if etype == "speech_output_cancelled" and data.get("output_id") in self._preempted_ids:
                    # 抢占已经计过一次，这里跳过，避免"被打断"重复计数
                    self._preempted_ids.discard(data.get("output_id"))
                else:
                    if etype == "speech_output_cancelled":
                        self._stats(data.get("speaker", "")).interrupted += 1
                self._close_output(data.get("output_id"), now)
            elif etype == "speech_output_queued":
                self._stats(data.get("speaker", "")).queued += 1
            elif etype == "speech_output_dropped":
                self._stats(data.get("speaker", "")).dropped += 1
            elif etype == "speech_output_preempted":
                # 抢占事件描述的是"谁抢到了"，被抢的那个记在 preempted_speaker 上
                self._stats(data.get("speaker", "")).preempted += 1
                preempted_id = data.get("preempted_output_id")
                if preempted_id:
                    self._preempted_ids.add(preempted_id)
                    self._close_output(preempted_id, now)
            elif etype == "speech_output_cooldown_blocked":
                self._stats(data.get("speaker", "")).cooldown_blocked += 1
            elif etype == self.STATE_EVENT:
                self._switch_state(data.get("new", ""), now)

    def _close_output(self, output_id, now: float):
        """结算一次发言的时长。"""
        if not output_id:
            return
        opened = self._open.pop(output_id, None)
        if not opened:
            return
        speaker, started_at = opened
        duration = max(0.0, now - started_at)
        self._stats(speaker).total_speech_s += duration

    def _switch_state(self, new_state: str, now: float):
        if self._state_entered_at is not None and self._current_state:
            self._state_durations[self._current_state] = (
                self._state_durations.get(self._current_state, 0.0) + (now - self._state_entered_at)
            )
        self._current_state = new_state or ""
        self._state_entered_at = now

    # ── 输出 ────────────────────────────────────────────────────────────
    def summary(self) -> dict:
        """结构化结果，供测试或写入复盘文件使用。"""
        with self._lock:
            now = time.time()
            speakers = {
                name: {
                    "starts": st.starts,
                    "total_speech_s": round(st.total_speech_s, 3),
                    "queued": st.queued,
                    "dropped": st.dropped,
                    "interrupted": st.interrupted,
                    "preempted": st.preempted,
                    "cooldown_blocked": st.cooldown_blocked,
                }
                for name, st in self._speakers.items()
            }
            total_speech = sum(st.total_speech_s for st in self._speakers.values())
            return {
                "elapsed_s": round(now - self._started_at, 3),
                "speakers": speakers,
                "total_speech_s": round(total_speech, 3),
                "open_outputs": len(self._open),
                "viewer_messages": self._viewer_messages,
                "viewer_by_source": dict(self._viewer_by_source),
                "state_durations": {k: round(v, 3) for k, v in self._state_durations.items()},
            }

    def report(self) -> str:
        """人类可读的复盘报告。"""
        snap = self.summary()
        total_speech = snap["total_speech_s"]
        lines = ["=== 直播复盘报告 ===", f"统计时长: {snap['elapsed_s']}s"]

        lines.append("")
        lines.append("[发言分布]")
        ordered = sorted(
            snap["speakers"].items(), key=lambda kv: kv[1]["total_speech_s"], reverse=True
        )
        any_speech = any(st["starts"] for _, st in ordered)
        if not any_speech:
            lines.append("  （本场没有发言记录）")
        # 累计时长极小时（无声干跑、或 TTS 关闭）按时长算占比会退化成无意义的
        # "100% / 0%"——分母被 round 到 0 之后仍可能被某一个人的微小非零值撑起来。
        # 此时改用发言次数算占比，并在报告里写明口径，避免读报告的人被误导。
        by_count = total_speech <= 1e-6
        total_starts = sum(st["starts"] for _, st in ordered)
        if by_count and total_starts:
            lines.append("  （本场累计时长为 0，占比按发言次数计算）")
        for name, st in ordered:
            if by_count:
                share = (st["starts"] / total_starts * 100) if total_starts else 0.0
            else:
                share = (st["total_speech_s"] / total_speech * 100) if total_speech else 0.0
            lines.append(
                f"  {name:<10} 发言 {st['starts']} 次   累计 {st['total_speech_s']:.2f}s   "
                f"占比 {share:.0f}%"
            )

        lines.append("")
        lines.append("[抢麦冲突]")
        queued = sum(st["queued"] for _, st in ordered)
        dropped = sum(st["dropped"] for _, st in ordered)
        interrupted = sum(st["interrupted"] for _, st in ordered)
        preempted = sum(st["preempted"] for _, st in ordered)
        cooled = sum(st["cooldown_blocked"] for _, st in ordered)
        lines.append(f"  排队等待:     {queued} 次")
        lines.append(f"  被丢弃:       {dropped} 次")
        lines.append(f"  被打断:       {interrupted} 次")
        lines.append(f"  优先级抢占:   {preempted} 次")
        lines.append(f"  冷却拦截:     {cooled} 次")
        lines.append(f"  未结束发言:   {snap['open_outputs']} 次")

        lines.append("")
        lines.append("[观众消息]")
        lines.append(f"  共收到 {snap['viewer_messages']} 条")
        for src, cnt in sorted(snap["viewer_by_source"].items()):
            lines.append(f"    {src}: {cnt}")

        if snap["state_durations"]:
            lines.append("")
            lines.append("[状态时间线]")
            for state, secs in sorted(
                snap["state_durations"].items(), key=lambda kv: kv[1], reverse=True
            ):
                lines.append(f"  {state:<16} {secs:.2f}s")

        return "\n".join(lines)
