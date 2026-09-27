"""duplex 语音层（A 模式：台词由快脑生成，这里只负责「发声」与「收音」）。

**在架构里的位置**：替换掉「独立 TTS + ASR」这一段，其余全不动——
发言权（仲裁器）、轮换与 @点名（调度器）、复盘指标（可观测性）、
搭档镜像（conversation）都还是本仓库自己的编排。

```
快脑生成台词 → conversation/lumi_tts.speak 按句切
                                    ↓  feed(句)
                            DuplexEmitter（本模块）
                                    ↓ speech_text_buffer.append/commit
                            DuplexSession（duplex_client.py）
                                    ↓ response.output_audio.delta
                            DuplexAudioPlayer → 角色虚拟声卡 / WAV（无声环境）
```

**为什么这样做**：`speak()` 已经在按句切文本并调用发声器的 `feed/finish/abort`，
所以只要提供一个满足 `tts_emitter.TtsEmitter` 接口的实现，
就能把整条生成链路原封不动地接到新协议上（见 docs/CREDENTIALS.md §5）。
"""
from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

from launcher.duplex_client import DuplexConfig, DuplexSession
from tts_emitter import TtsEmitter

SENTINEL = None


class DuplexAudioPlayer:
    """把 PCM 写进角色专属虚拟声卡；打不开声卡时落盘成 WAV（便于无声环境验证）。

    为什么要有独立线程 + 队列：`response.output_audio.delta` 回调运行在会话的
    事件循环线程里，**回调必须尽快返回**；写声卡是阻塞调用，直接写会拖慢收包。
    """

    def __init__(self, *, pa, cable_index, rate: int = 24000, speaker: str = "",
                 wav_dir: str | None = None, log_fn=print):
        self._pa = pa
        self._rate = rate
        self._speaker = speaker or "speaker"
        self._log = log_fn
        self._q: queue.Queue = queue.Queue()
        self._stream = None
        self._thread = None
        self._closed = False
        self.bytes_in = 0
        self.bytes_played = 0
        self._wav = None
        self._wav_path = None

        if pa is not None and cable_index is not None:
            try:
                self._stream = pa.open(format=pa.get_sample_size(8) and 8,  # 8 = paInt16
                                       channels=1, rate=rate, output=True,
                                       output_device_index=cable_index,
                                       frames_per_buffer=rate // 5)       # 200ms，抗抖动
                self._log(f"[duplex·播放] {self._speaker} → 声卡 index={cable_index}")
            except Exception as e:
                self._log(f"[duplex·播放] {self._speaker} 声卡打开失败({e})，改为落盘 WAV")
                self._stream = None
        if self._stream is None and wav_dir:
            try:
                Path(wav_dir).mkdir(parents=True, exist_ok=True)
                self._wav_path = str(Path(wav_dir) / f"duplex_{self._speaker}_"
                                                   f"{time.strftime('%H%M%S')}.pcm")
                self._wav = open(self._wav_path, "wb")
                self._log(f"[duplex·播放] {self._speaker} 无可用声卡 → 音频落盘 {self._wav_path}")
            except Exception as e:
                # 落盘失败不该让整场直播起不来：退化为"只统计不输出"
                self._wav = None
                self._wav_path = None
                self._log(f"[duplex·播放] {self._speaker} 音频落盘失败({e})，本角色仅统计不入盘")

        if self._stream is not None or self._wav is not None:
            self._thread = threading.Thread(target=self._worker, daemon=True,
                                            name=f"duplex-player-{self._speaker}")
            self._thread.start()

    # ── 供会话回调调用（必须快）──────────────────────────────────────
    def write(self, pcm: bytes):
        if self._closed or not pcm:
            return
        self.bytes_in += len(pcm)
        self._q.put(pcm)

    def flush(self):
        """打断时清空待播队列（避免打断后还在念旧内容）。"""
        dropped = 0
        while True:
            try:
                self._q.get_nowait()
                dropped += 1
            except queue.Empty:
                break
        if dropped:
            self._log(f"[duplex·播放] {self._speaker} 打断：丢弃 {dropped} 段待播音频")

    def wait_drained(self, timeout: float = 10.0) -> bool:
        """等队列里的音频写完（供 finish() 判断"真的播完了"）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._q.empty():
                return True
            time.sleep(0.05)
        return False

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._q.put(SENTINEL)
        if self._thread is not None:
            self._thread.join(timeout=5)
        try:
            if self._stream is not None:
                self._stream.stop_stream()
                self._stream.close()
        except Exception:
            pass
        try:
            if self._wav is not None:
                self._wav.close()
        except Exception:
            pass

    @property
    def wav_path(self):
        return self._wav_path

    # ── 内部 ──────────────────────────────────────────────────────────
    def _worker(self):
        while not self._closed:
            item = self._q.get()
            if item is SENTINEL:
                break
            try:
                if self._stream is not None:
                    self._stream.write(item)
                elif self._wav is not None:
                    self._wav.write(item)
                    self._wav.flush()
                self.bytes_played += len(item)
            except Exception as e:
                self._log(f"[duplex·播放] {self._speaker} 写出失败：{e}")
                break


class DuplexEmitter(TtsEmitter):
    """把 `speak()` 逐句喂进来的文本，经 duplex 会话合成并播放。

    与 `IndependentTTSEmitter` 的语义一致：
    - `feed(sentence)`：送一句；首句建立发声流并返回 stream_task_id
    - `finish()`：收尾并**阻塞到音频真正播完**（仲裁器据此才交还发言权）
    - `abort()`：被打断 → 本地停播 + 丢弃后续音频
    """

    def __init__(self, *, session: DuplexSession, player: DuplexAudioPlayer,
                 speaker: str, log_fn=print):
        self._session = session
        self._player = player
        self._speaker = speaker
        self._log = log_fn
        self._speech_id = None
        self._last_finish_ok = None

    def feed(self, sentence: str):
        if self._speech_id is None:
            self._speech_id = self._session.begin_speech()
            self._log(f"[duplex·说] {self._speaker} 开始一段播报 speech={self._speech_id[:8]}…")
        self._session.append_speech_text(self._speech_id, sentence)
        return self._speech_id

    def finish(self) -> None:
        if self._speech_id is None:
            return
        self._session.commit_speech_text(self._speech_id)
        ok = self._session.wait_speech_done(timeout=30.0)
        self._player.wait_drained(timeout=10.0)
        self._last_finish_ok = ok
        if not ok:
            self._log(f"[duplex·说] {self._speaker} 等待出声结束超时（30s）")
        self._log(f"[duplex·说] {self._speaker} 播报结束，"
                  f"本段音频 {self._session.audio_bytes} 字节累计")
        self._speech_id = None

    def abort(self) -> None:
        if self._speech_id is not None:
            self._session.abort_speech(self._speech_id)
            self._player.flush()
            self._speech_id = None

    @property
    def stream_task_id(self):
        return self._speech_id


class DuplexVoice:
    """按角色管理 duplex 会话与播放器，并对外提供发声器工厂。"""

    def __init__(self, *, api_key: str, characters: list, log_fn=print,
                 pa=None, cable_indices: dict | None = None,
                 voice_of=None, model: str = None, instructions_of=None,
                 wav_dir: str | None = None, enable_mic: bool = False):
        self._api_key = api_key
        self._characters = list(characters)
        self._log = log_fn
        self._pa = pa
        self._cables = dict(cable_indices or {})
        self._voice_of = voice_of or (lambda _sp: "")
        self._model = model
        self._instructions_of = instructions_of or (lambda _sp: "")
        self._wav_dir = wav_dir
        self._enable_mic = enable_mic
        self.sessions: dict = {}
        self.players: dict = {}
        self._last_speaker: str | None = None

    # ── 生命周期 ──────────────────────────────────────────────────────
    def start(self) -> bool:
        from launcher.duplex_client import DEFAULT_MODEL, DEFAULT_VOICE
        ok_any = False
        for sp in self._characters:
            voice = self._voice_of(sp) or DEFAULT_VOICE
            cfg = DuplexConfig(
                api_key=self._api_key,
                model=self._model or DEFAULT_MODEL,
                voice=voice,
                instructions=self._instructions_of(sp) or
                "你是一个正在直播的 AI 主播，说话自然、口语化，一次只说一句短话。",
            )
            player = DuplexAudioPlayer(pa=self._pa, cable_index=self._cables.get(sp),
                                       speaker=sp, wav_dir=self._wav_dir, log_fn=self._log)

            def _on_audio(pcm, _p=player):
                _p.write(pcm)

            sess = DuplexSession(cfg, on_audio=_on_audio, log_fn=self._log)
            if sess.start(timeout=20):
                self.sessions[sp] = sess
                self.players[sp] = player
                self._log(f"[duplex·会话] {sp} 就绪（voice={voice} dialog_id={sess.dialog_id}）")
                ok_any = True
            else:
                self._log(f"✗ [duplex·会话] {sp} 未能就绪（凭证/网络？先跑 "
                          f"`python -m launcher.duplex_client` 验证）")
                player.close()
        return ok_any

    def close(self):
        for sp, sess in list(self.sessions.items()):
            try:
                sess.close()
            except Exception:
                pass
            self._log(f"[duplex·会话] {sp} 已关闭")
        for sp, player in list(self.players.items()):
            try:
                player.close()
                if player.wav_path:
                    self._log(f"[duplex·播放] {sp} 音频已落盘：{player.wav_path}")
            except Exception:
                pass
        self.sessions.clear()
        self.players.clear()

    # ── 给 lumi_tts 用的发声器工厂 ────────────────────────────────────
    def make_emitter(self, speaker: str, cable_index=None):
        """工厂签名与 `lumi_tts.set_emitter_factory` 一致；返回 None 表示不由本层处理。"""
        sess = self.sessions.get(speaker)
        if sess is None:
            return None
        self._last_speaker = speaker
        return DuplexEmitter(session=sess, player=self.players[speaker],
                             speaker=speaker, log_fn=self._log)

    # ── 便捷接口（调试 / 自测）────────────────────────────────────────
    def say(self, speaker: str, text: str, *, wait: float = 30.0) -> bool:
        sess = self.sessions.get(speaker)
        if sess is None:
            return False
        em = DuplexEmitter(session=sess, player=self.players[speaker],
                           speaker=speaker, log_fn=self._log)
        em.feed(text)
        em.finish()
        return bool(em._last_finish_ok)

    def speech_seconds(self, speaker: str) -> float:
        """该角色累计播放时长（按真实音频字节算）——比"文本长度估算"可靠。"""
        p = self.players.get(speaker)
        return (p.bytes_in / 2 / 24000) if p else 0.0
