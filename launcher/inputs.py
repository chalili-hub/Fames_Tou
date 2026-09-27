"""观众输入源与麦克风输入。

两件事：
1. **观众消息**（弹幕/SC/礼物/进房…）→ 进调度器队列 + 观测性埋点。
   记忆落库**不在这里做**：`conversation.chat_and_speak`
   会在拿到 `viewer_identity_key` 后自己写（conversation.py:678-688），
   这里再写一次会重复入库。
2. **麦克风** → 一条常驻线程同时做两件事：喂 ASR、做 VAD 打断。
   （上游是在每次 speak 时起一个 interrupt_monitor 线程；本层把两者收敛到
    一个线程，避免两个线程抢同一个输入设备——见 helpers.interrupt_monitor 的说明。）
"""
from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass, field

# 与 speaker_scheduler._extract_routing_text 认识的输入源前缀保持一致
PREFIX_BY_SOURCE = {
    "danmaku": "弹幕",
    "super_chat": "SC",
    "gift": "礼物",
    "guard_buy": "上舰",
    "enter_room": "进房",
    "interact": "互动",
    "voice": "语音",
}


@dataclass
class ViewerItem:
    body: str                      # 正文（不含来源前缀）
    source: str = "danmaku"
    label: str = ""                # 显示名
    uid: int = 0                   # B 站数字 UID（0 = 无）
    extra: dict = field(default_factory=dict)

    def as_scheduler_text(self) -> str:
        """拼成调度器期望的形态：`弹幕：名字：正文`。

        `speaker_scheduler._extract_routing_text` 会剥掉前缀、只对正文做 @ 判断，
        所以这里的格式必须与它对齐（`speaker_scheduler.py:44-54`）。
        """
        prefix = PREFIX_BY_SOURCE.get(self.source, "弹幕")
        who = self.label or "观众"
        return f"{prefix}：{who}：{self.body}"

    def identity_key(self) -> str:
        from memory.identity import bili_identity, legacy_identity
        return bili_identity(self.uid) if self.uid else legacy_identity(self.label or "未知")


def enqueue_item(item: ViewerItem, *, scheduler, analytics=None, log_fn=None) -> None:
    """入队（不打断当前发言）+ 观测性埋点。"""
    scheduler.enqueue_input(
        text=item.as_scheduler_text(),
        source=item.source,
        speaker=item.label,
        display_text=item.body,
        label=item.label,
        uid=item.uid,
    )
    if analytics is not None:
        try:
            analytics.record_viewer_input(item.source)
        except Exception as e:
            (log_fn or print)(f"[观测] 观众消息埋点失败：{e}")


# ── 输入源 ──────────────────────────────────────────────────────────────

class BaseSource:
    def start(self):
        return self

    def stop(self):
        return None


class NullSource(BaseSource):
    """没有输入源：适合干跑或只有主动发言的场景。"""


class ScriptedSource(BaseSource):
    """按脚本喂入（干跑/自测用）：每条之间 sleep interval 秒。"""

    def __init__(self, items: list, *, interval: float = 0.2, delay: float = 0.0,
                 scheduler=None, analytics=None, log_fn=None):
        self._items = list(items)
        self._interval = interval
        self._delay = delay
        self._scheduler = scheduler
        self._analytics = analytics
        self._log = log_fn or print
        self._thread = None

    def start(self):
        # 幂等：`build_engine` 会构造并启动 source，`lumi.main` 随后还会再调一次 start()。
        # 没有这道保护就会起两个线程、每条弹幕入队两次
        # （实测症状：复盘报告里观众消息数翻倍）。
        if self._thread is not None:
            return self

        def _run():
            if self._delay:
                time.sleep(self._delay)
            for it in self._items:
                if it is None:
                    continue
                enqueue_item(it, scheduler=self._scheduler, analytics=self._analytics,
                             log_fn=self._log)
                self._log(f"  [输入] {it.as_scheduler_text()}")
                time.sleep(self._interval)
        self._thread = threading.Thread(target=_run, daemon=True, name="scripted-source")
        self._thread.start()
        return self

    def stop(self):
        return None


class StdinSource(BaseSource):
    """命令行模拟输入：直接打字回车即当成一条弹幕。

    支持两种写法：
      - `讲个冷笑话`              → 普通弹幕，label=你
      - `tou:讲个冷笑话`          → 指定显示名
      - `SC:名字:内容`            → 指定来源与显示名（来源可用 弹幕/SC/礼物/上舰/进房/互动）
    """
    _SRC_ALIAS = {v: k for k, v in PREFIX_BY_SOURCE.items()}

    def __init__(self, *, scheduler, analytics=None, log_fn=None, label: str = "你"):
        self._scheduler = scheduler
        self._analytics = analytics
        self._log = log_fn or print
        self._label = label
        self._thread = None

    def _parse(self, line: str) -> ViewerItem:
        parts = line.split("：") if "：" in line else line.split(":")
        if len(parts) >= 3 and parts[0] in self._SRC_ALIAS:
            return ViewerItem(body="：".join(parts[2:]), source=self._SRC_ALIAS[parts[0]],
                              label=parts[1].strip() or self._label)
        if len(parts) == 2:
            return ViewerItem(body=parts[1], source="danmaku", label=parts[0].strip() or self._label)
        return ViewerItem(body=line, source="danmaku", label=self._label)

    def start(self):
        if self._thread is not None:      # 幂等，理由同 ScriptedSource
            return self

        def _run():
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                if line in ("/quit", "/exit"):
                    from launcher.registry import RUNTIME
                    RUNTIME.stop_event.set()
                    break
                item = self._parse(line)
                enqueue_item(item, scheduler=self._scheduler, analytics=self._analytics,
                             log_fn=self._log)
        self._thread = threading.Thread(target=_run, daemon=True, name="stdin-danmaku")
        self._thread.start()
        return self

    def stop(self):
        return None


class BiliDanmakuSource(BaseSource):
    """B 站弹幕适配器接口（**未实现**）。

    为什么留空而不是猜实现：接入需要你的开放平台凭证与协议细节，
    写一个"看起来能跑"的假实现比留空更糟。要接的时候在这里补三件事：

      1. 连接与鉴权（开放平台长连接 / 或第三方弹幕库）；
      2. 事件 → ViewerItem 的映射：
         - 弹幕 DANMU_MSG      → ViewerItem(source="danmaku", uid=..., label=..., body=...)
         - 付费 SC SUPER_CHAT  → source="super_chat"（会被优先级队列全保、可抢占话筒）
         - 上舰 GUARD_BUY      → source="guard_buy"（记忆层有确定性守卫专门兜它）
         - 礼物 SEND_GIFT      → source="gift"
      3. 每条都调 `enqueue_item(...)`（它负责入队 + 观测性埋点）。

    提示：`uid` 一定要带上（B 站数字 UID）——记忆系统按 `bili:{uid}` 分人，
    没有 uid 只能退化成 `legacy:{昵称}`，观众改名就丢记忆。
    """

    def __init__(self, *_, **__):
        raise NotImplementedError(
            "B 站弹幕接入未实现：本层提供适配器接口，接入方式见 BiliDanmakuSource 的文档字符串"
        )


def build_source(cfg, *, scheduler, analytics, log_fn):
    if cfg.danmaku == "stdin":
        return StdinSource(scheduler=scheduler, analytics=analytics, log_fn=log_fn)
    if cfg.danmaku == "script":
        # 脚本化输入：用于自动化验收与录演示（无需人工打字）
        # interval 越大，越可能是"一条弹幕 → 一轮回应"；越小则会被合并成一轮批量回应。
        return ScriptedSource(DEFAULT_SCRIPT, interval=cfg.script_interval, delay=0.5,
                              scheduler=scheduler, analytics=analytics, log_fn=log_fn).start()
    if cfg.danmaku == "bili":
        return BiliDanmakuSource()
    return NullSource()


# 默认脚本：覆盖「普通弹幕 / @点名 / 付费消息 / 再追问」四类，够验收主链路
DEFAULT_SCRIPT = [
    ViewerItem(body="晚上好，今天播到几点呀？", source="danmaku", label="mona", uid=1001),
    ViewerItem(body="tou 讲个冷笑话", source="danmaku", label="rin", uid=1002),
    ViewerItem(body="支持一下，加油", source="super_chat", label="kane", uid=1003),
    ViewerItem(body="fames 你刚才说的那个我再问一下", source="danmaku", label="mona", uid=1001),
]


# ── 麦克风：喂 ASR + VAD 打断 ────────────────────────────────────────────

class MicPump:
    """常驻麦克风线程。

    职责：
      - 16kHz 单声道采集，100ms 一帧（与 lumi_asr 的聚合粒度一致）
      - 推给 ASR（`asr.push_audio`），一轮说话结束时取终稿 → 当作用户输入入队
      - VAD 检测到有人开口，且此刻 AI 正在说话 → `lumi_tts.interrupt_current_speech()`

    ⚠️ 本线程在无声卡/无麦克风的机器上无法验证（本层已在代码里做了失败降级：
    打不开设备就整条链路静默不启用，不影响文本输入与发声）。
    """

    FRAME_MS = 100
    RATE = 16000

    def __init__(self, *, pa, asr, scheduler, analytics, vad, log_fn,
                 input_index=None, source: str = "voice"):
        self._pa = pa
        self._asr = asr
        self._scheduler = scheduler
        self._analytics = analytics
        self._vad = vad
        self._log = log_fn
        self._input_index = input_index
        self._source = source
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._pa is None:
            self._log("[麦克风] 没有音频实例，跳过麦克风链路")
            return self
        self._thread = threading.Thread(target=self._run, daemon=True, name="mic-pump")
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _resolve_input(self):
        try:
            if self._input_index is not None:
                return self._input_index
            return self._pa.get_default_input_device_info()["index"]
        except Exception as e:
            self._log(f"[麦克风] 找不到输入设备：{e}")
            return None

    def _run(self):
        import numpy as np
        import lumi_tts

        idx = self._resolve_input()
        if idx is None:
            return
        frames = int(self.RATE * self.FRAME_MS / 1000)
        try:
            stream = self._pa.open(format=self._pa.get_sample_size(1) and 8,  # paInt16 = 8
                                   channels=1, rate=self.RATE, input=True,
                                   input_device_index=idx, frames_per_buffer=frames)
        except Exception as e:
            self._log(f"[麦克风] 打开失败，麦克风链路不启用：{e}")
            return

        in_speech = False
        while not self._stop.is_set():
            try:
                raw = stream.read(frames, exception_on_overflow=False)
            except Exception as e:
                self._log(f"[麦克风] 读取失败，退出麦克风链路：{e}")
                break
            audio = np.frombuffer(raw, dtype=np.int16).astype("float32") / 32768.0
            speech = self._vad.is_speech(audio)

            if speech and not in_speech:
                in_speech = True
                # 有人开口 → 打断 AI（barge-in）
                if getattr(lumi_tts.tts_state, "is_speaking", False):
                    self._log("     [打断] 检测到人声 → 打断当前发言")
                    lumi_tts.interrupt_current_speech()
                if self._asr is not None:
                    try:
                        self._asr.promote_and_start_turn()
                    except Exception as e:
                        self._log(f"[ASR] 开始一轮失败：{e}")

            if self._asr is not None:
                try:
                    self._asr.push_audio(audio)
                except Exception as e:
                    self._log(f"[ASR] 推流失败：{e}")

            if in_speech and not speech:
                in_speech = False
                if self._asr is not None:
                    try:
                        readout = self._asr.wait_for_final(timeout_ms=800)
                        text = (getattr(readout, "text", "") or "").strip()
                        if text:
                            enqueue_item(ViewerItem(body=text, source=self._source, label="你"),
                                         scheduler=self._scheduler,
                                         analytics=self._analytics, log_fn=self._log)
                        self._asr.retire_active()
                        self._asr.arm_next()
                    except Exception as e:
                        self._log(f"[ASR] 收尾失败：{e}")
        try:
            stream.stop_stream()
            stream.close()
        except Exception:
            pass
