"""Speech output arbitration.

This is the runtime implementation of the "表现调度" layer from the system
architecture: one effective speech output at a time, with explicit queue /
drop / interrupt policy.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional


POLICY_QUEUE = "queue_after_current"
POLICY_DROP = "drop_if_busy"
POLICY_INTERRUPT = "interrupt_current"
# 优先级抢占：带更高 ``priority`` 的请求可以不等当前发言结束，直接把发言权拿过来。
# 用于付费互动（SC / 上舰 / 礼物）——它们不能因为两个角色正在闲聊就被漏掉。
POLICY_PRIORITY = "priority_preempt"


@dataclass
class SpeechOutput:
    output_id: str
    speaker: str
    source: str
    policy: str
    priority: int = 0
    started_at: float = field(default_factory=time.time)
    task_id: Optional[str] = None
    cancelled: bool = False
    reason: str = ""


class SpeechOutputArbiter:
    def __init__(self, *, event_bus=None, log_fn: Callable[[str], None] | None = None,
                 cooldown_s: float = 0.0):
        self._lock = threading.RLock()
        self._counter = 0
        self._current: SpeechOutput | None = None
        self._cancelled: set[str] = set()
        self._queue: queue.Queue[dict] = queue.Queue()
        self._cancel_callback: Callable[[SpeechOutput, str], None] | None = None
        self._event_bus = event_bus
        self._log_fn = log_fn or (lambda _msg: None)
        # 连麦冷却：同一个角色刚交还发言权后，在 cooldown_s 秒内不能立刻再次开口。
        # 0 表示禁用（默认）。作用是防止双角色场景下话说得多的那个把场子全占了。
        self._cooldown_s = max(0.0, float(cooldown_s))
        self._last_release_at: dict[str, float] = {}

    def set_cooldown(self, cooldown_s: float):
        """调整连麦冷却时长（秒）。传 0 关闭冷却。"""
        with self._lock:
            self._cooldown_s = max(0.0, float(cooldown_s))

    def cooldown_s(self) -> float:
        with self._lock:
            return self._cooldown_s

    def cooldown_remaining(self, speaker: str, *, now: float | None = None) -> float:
        """返回该角色还需要等多少秒才能重新开口（0 = 现在就可以）。"""
        with self._lock:
            return self._cooldown_remaining_locked(speaker, now=now)

    def _cooldown_remaining_locked(self, speaker: str, *, now: float | None = None) -> float:
        if self._cooldown_s <= 0 or not speaker:
            return 0.0
        last = self._last_release_at.get(speaker)
        if last is None:
            return 0.0
        elapsed = (now if now is not None else time.time()) - last
        remaining = self._cooldown_s - elapsed
        return remaining if remaining > 0 else 0.0

    def _note_release_locked(self, output: SpeechOutput | None):
        """记录某个角色的发言权交还时刻，供冷却判定使用。"""
        if output and output.speaker:
            self._last_release_at[output.speaker] = time.time()

    def configure(self, *, event_bus=None, log_fn=None,
                  cancel_callback: Callable[[SpeechOutput, str], None] | None = None):
        with self._lock:
            if event_bus is not None:
                self._event_bus = event_bus
            if log_fn is not None:
                self._log_fn = log_fn
            if cancel_callback is not None:
                self._cancel_callback = cancel_callback

    def request_start(self, *, speaker: str, source: str,
                      policy: str = POLICY_QUEUE,
                      priority: int = 0,
                      reason: str = "",
                      ignore_cooldown: bool = False) -> SpeechOutput | None:
        with self._lock:
            if self._current and self.is_busy_locked():
                if policy == POLICY_DROP:
                    self._publish("speech_output_dropped", {
                        "speaker": speaker, "source": source, "policy": policy,
                        "current_output_id": self._current.output_id,
                    })
                    return None
                if policy == POLICY_PRIORITY and priority > self._current.priority:
                    # 优先级更高 → 抢占：先广播抢占事件，再把发言权夺过来
                    preempted = self._current
                    self._publish("speech_output_preempted", {
                        "speaker": speaker, "source": source, "priority": priority,
                        "preempted_speaker": preempted.speaker,
                        "preempted_output_id": preempted.output_id,
                        "preempted_priority": preempted.priority,
                    })
                    self.cancel_current_locked(reason or f"preempted_by_{source}")
                elif policy in (POLICY_QUEUE, POLICY_PRIORITY):
                    # 普通排队；优先级策略但优先级不够时同样退化为排队，不打断
                    self._queue.put({
                        "speaker": speaker, "source": source,
                        "policy": policy, "priority": priority, "reason": reason,
                    })
                    self._publish("speech_output_queued", {
                        "speaker": speaker, "source": source,
                        "current_output_id": self._current.output_id,
                        "queue_size": self._queue.qsize(),
                    })
                    return None
                elif policy == POLICY_INTERRUPT:
                    self.cancel_current_locked(reason or f"interrupted_by_{source}")

            # 连麦冷却：刚交还发言权的角色不能立刻再次开口，避免单个角色霸场
            if not ignore_cooldown:
                remaining = self._cooldown_remaining_locked(speaker)
                if remaining > 0:
                    self._publish("speech_output_cooldown_blocked", {
                        "speaker": speaker, "source": source,
                        "remaining_s": round(remaining, 3),
                        "cooldown_s": self._cooldown_s,
                    })
                    return None

            output = self._new_output_locked(speaker=speaker, source=source,
                                             policy=policy, priority=priority)
            self._publish("speech_output_started", self._event_data(output))
            return output

    def mark_task_id(self, output_id: str, task_id: str | None):
        if not output_id or not task_id:
            return
        with self._lock:
            if self._current and self._current.output_id == output_id:
                self._current.task_id = task_id

    def mark_done(self, output_id: str | None):
        if not output_id:
            return
        with self._lock:
            if self._current and self._current.output_id == output_id:
                done = self._current
                self._current = None
                self._note_release_locked(done)
                self._publish("speech_output_done", self._event_data(done))
            self._cancelled.discard(output_id)

    def fail_current(self, reason: str = "failed", *,
                     task_id: str | None = None,
                     output_id: str | None = None) -> SpeechOutput | None:
        """Release the current output after a transport/generation failure."""
        with self._lock:
            current = self._current
            if not current:
                return None
            if output_id and current.output_id != output_id:
                return None
            if task_id and current.task_id and current.task_id != task_id:
                return None
            failed = current
            failed.reason = reason
            self._current = None
            self._note_release_locked(failed)
            self._cancelled.discard(failed.output_id)
            self._publish("speech_output_failed", {
                **self._event_data(failed),
                "reason": reason,
            })
            return failed

    def cancel_current(self, reason: str = "cancelled") -> SpeechOutput | None:
        with self._lock:
            return self.cancel_current_locked(reason)

    def cancel_current_locked(self, reason: str) -> SpeechOutput | None:
        if not self._current:
            return None
        old = self._current
        old.cancelled = True
        old.reason = reason
        self._cancelled.add(old.output_id)
        self._current = None
        self._note_release_locked(old)
        self._publish("speech_output_cancelled", {
            **self._event_data(old),
            "reason": reason,
        })
        callback = self._cancel_callback
        if callback:
            try:
                callback(old, reason)
            except Exception as exc:
                self._log_fn(f"[speech_arbiter] cancel callback failed: {exc}")
        return old

    def is_current(self, output_id: str | None) -> bool:
        if not output_id:
            return False
        with self._lock:
            return bool(
                self._current
                and self._current.output_id == output_id
                and output_id not in self._cancelled
            )

    def is_current_task(self, task_id: str | None) -> bool:
        if not task_id:
            return True
        with self._lock:
            return bool(
                self._current
                and self._current.task_id == task_id
                and self._current.output_id not in self._cancelled
            )

    def current_task_id(self) -> str | None:
        with self._lock:
            return self._current.task_id if self._current else None

    def is_cancelled(self, output_id: str | None) -> bool:
        if not output_id:
            return False
        with self._lock:
            return output_id in self._cancelled

    def is_busy(self) -> bool:
        with self._lock:
            return self.is_busy_locked()

    def is_busy_locked(self) -> bool:
        return self._current is not None

    def current(self) -> SpeechOutput | None:
        with self._lock:
            return self._current

    def finish_current_if_idle(self, *, max_age_s: float,
                               is_transport_busy_fn: Callable[[], bool],
                               reason: str = "transport_idle") -> SpeechOutput | None:
        with self._lock:
            output = self._current
            if not output:
                return None
            age_s = time.time() - output.started_at
            if age_s < max_age_s:
                return None
        try:
            if is_transport_busy_fn():
                return None
        except Exception as exc:
            self._log_fn(f"[speech_arbiter] transport busy check failed: {exc}")
            return None
        with self._lock:
            if not self._current or self._current.output_id != output.output_id:
                return None
            self._current = None
            self._note_release_locked(output)
            self._cancelled.discard(output.output_id)
            self._publish("speech_output_done", {
                **self._event_data(output),
                "reason": reason,
                "age_s": age_s,
            })
            return output

    def queued_count(self) -> int:
        return self._queue.qsize()

    def _new_output_locked(self, *, speaker: str, source: str, policy: str,
                           priority: int = 0) -> SpeechOutput:
        self._counter += 1
        output = SpeechOutput(
            output_id=f"speech_{int(time.time() * 1000)}_{self._counter}",
            speaker=speaker or "",
            source=source or "",
            policy=policy or POLICY_QUEUE,
            priority=int(priority or 0),
        )
        self._current = output
        return output

    def _event_data(self, output: SpeechOutput) -> dict:
        return {
            "output_id": output.output_id,
            "speaker": output.speaker,
            "source": output.source,
            "policy": output.policy,
            "priority": output.priority,
            "task_id": output.task_id,
            "started_at": output.started_at,
        }

    def _publish(self, event_type: str, data: dict):
        if self._event_bus is None:
            return
        try:
            self._event_bus.publish(event_type, data, source="speech_output_arbiter")
        except Exception as exc:
            self._log_fn(f"[speech_arbiter] publish {event_type} failed: {exc}")


arbiter = SpeechOutputArbiter()
