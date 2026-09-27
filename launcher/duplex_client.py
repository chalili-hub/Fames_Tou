"""火山引擎「端到端实时语音（duplex）」协议客户端 —— 本仓库自己的最小实现。

**为什么不用仓库里的 `realtime_chat.py`**：那是上游按**旧版二进制协议**写的
（`/api/v3/realtime/dialogue` + 4 个鉴权头 + gzip 二进制帧 + `model=2.2.0.0`），
而控制台现在只发**新版 duplex 协议**的 API Key（`/api/v3/duplex/realtime/dialogue`
+ 单个 `X-Api-Key` 头 + 纯 JSON 事件 + `model=1.2.6.1`）。实测：拿新 Key 打旧端点必 401。
详见 docs/CREDENTIALS.md。

**它在引擎里的角色（A 模式）**：只负责「耳朵 + 嗓子」。
台词由快脑（LLM）生成 → 经 `speech_text_buffer.*` 交给会话合成 → 音频回传播放；
麦克风音频经 `input_audio_buffer.*` 上行，转写结果由 transcription 事件回传。
发言权、抢占、冷却、游戏决策仍全部由本仓库的编排层掌握。

线程模型与 `realtime_chat` 保持一致：**一条后台线程 + 独立 asyncio 事件循环**，
对外暴露同步 API（内部用 `run_coroutine_threadsafe` 投递），
这样调用方（`lumi_tts` 的发声器、主循环）不需要理解 asyncio。

自测（不需要声卡，直接把音频存成 WAV）::

    python -m launcher.duplex_client --text "大家好，我是今晚的主播。" --out say.wav
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import threading
import time
import uuid
import wave
from dataclasses import dataclass, field
from typing import Callable, Optional

ENDPOINT_URL = "wss://openspeech.bytedance.com/api/v3/duplex/realtime/dialogue"
DEFAULT_MODEL = "1.2.6.1"
DEFAULT_VOICE = "zh_female_xiaohe_jupiter_bigtts"
OUTPUT_RATE = 24000
INPUT_RATE = 16000

# ── 客户端 → 服务端 ────────────────────────────────────────────────────
T_SESSION_CREATE = "session.create"
T_SESSION_UPDATE = "session.update"
T_SESSION_CLOSE = "session.close"
T_INPUT_AUDIO_APPEND = "input_audio_buffer.append"
T_INPUT_AUDIO_COMMIT = "input_audio_buffer.commit"
T_SPEECH_APPEND = "speech_text_buffer.append"
T_SPEECH_COMMIT = "speech_text_buffer.commit"

# ── 服务端 → 客户端 ────────────────────────────────────────────────────
T_SESSION_CREATED = "session.created"
T_SESSION_CLOSED = "session.closed"
T_AUDIO_STARTED = "response.output_audio.started"
T_AUDIO_DELTA = "response.output_audio.delta"
T_AUDIO_DONE = "response.output_audio.done"
T_TEXT_DELTA = "response.output_text.delta"
T_TEXT_DONE = "response.output_text.done"
T_TRANSCRIPT_COMPLETED = "conversation.item.input_audio_transcription.completed"
T_FUNCTION_CALL = "response.function_call_arguments.done"
T_CANCELED = "response.canceled"
T_RESPONSE_DONE = "response.done"
T_ERROR = "error"


def resolve_api_key(explicit: Optional[str] = None) -> str:
    """解析 duplex API Key。

    兼容顺序：显式参数 → `VOLC_DUPLEX_API_KEY`（新名，推荐）
    → `VOLC_DIALOG_ACCESS_KEY`（历史名，仓库早期按旧协议命名）。
    """
    for candidate in (explicit, os.environ.get("VOLC_DUPLEX_API_KEY"),
                      os.environ.get("VOLC_DIALOG_ACCESS_KEY")):
        if candidate and candidate.strip():
            return candidate.strip()
    return ""


@dataclass
class DuplexConfig:
    api_key: str = ""
    endpoint_url: str = ENDPOINT_URL
    model: str = DEFAULT_MODEL
    instructions: str = "你是一个正在直播的 AI 主播，说话自然、口语化，一次只说一句短话。"
    voice: str = DEFAULT_VOICE
    asr_format: str = "pcm"
    tts_format: str = "pcm_s16le"
    input_rate: int = INPUT_RATE
    output_rate: int = OUTPUT_RATE
    # 会话扩展：内容安全兜底话术（服务端命中审核时用它替代原回答）
    audit_response: str = "抱歉，这个问题我无法回答，你可以换个其他话题。"
    enable_tools: bool = False          # A 模式下工具由本地快脑管，默认不给服务端工具
    # 输入保活：A 模式只发文本不发音频，服务端在长时间没有音频输入后会报
    # `AudioServerNoAudioInputTooLongError`（实测约 90 秒），导致后续播报拿不到音频。
    # 这里定期送一帧静音把输入心跳维持住；设 0 可关闭。
    keepalive_seconds: float = 10.0


class DuplexSession:
    """一条 duplex 会话。线程安全：所有公开方法都可从任意线程调用。"""

    def __init__(self, cfg: DuplexConfig, *,
                 on_audio: Optional[Callable[[bytes], None]] = None,
                 on_text_done: Optional[Callable[[str], None]] = None,
                 on_transcript: Optional[Callable[[str], None]] = None,
                 on_event: Optional[Callable[[dict], None]] = None,
                 log_fn: Callable[[str], None] = print):
        self.cfg = cfg
        self._on_audio = on_audio
        self._on_text_done = on_text_done
        self._on_transcript = on_transcript
        self._on_event = on_event
        self._log = log_fn
        self.session_id = str(uuid.uuid4())
        self.dialog_id = ""
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ws = None
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._error: Optional[str] = None
        self._event_id = 0
        self._aborted_speeches: set = set()
        self._text_buf: list = []
        self._speech_pending: dict = {}      # speech_id -> 尚未发送的最后一句
        self._started = False
        # 「这一段真的说完了吗」的信号：A 模式下同一会话同时只有一段播报
        # （发言权由仲裁器保证），所以一个事件足够；speak() 的 finish() 靠它阻塞。
        self._audio_done = threading.Event()
        self._audio_started = threading.Event()
        self.audio_bytes = 0                 # 累计收到音频字节（可用于算真实发言时长）
        self.dropped_bytes = 0               # 因本地打断而丢弃的音频字节
        self.last_usage: dict = {}
        # ★ 关联键：服务端的音频事件**不带 speech_id**，只带 response_id /
        #   question_id（实测：response.output_audio.started 的字段里没有 speech_id）。
        #   所以"这段音频属于哪次播报"必须由客户端自己记：A 模式下同一会话
        #   同时只有一段播报（发言权由仲裁器保证），commit 时记下当前 speech_id 即可。
        self._active_speech_id: Optional[str] = None
        self._last_input_at = 0.0            # 上次上行音频的时刻（保活判据）
        self._keepalive_sent = 0

    # ── 生命周期 ──────────────────────────────────────────────────────
    def start(self, *, timeout: float = 20.0) -> bool:
        """建连并创建会话（阻塞直到 session.created 或超时）。"""
        if self._started:
            return self._ready.is_set()
        self._started = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True,
                                        name="duplex-session")
        self._thread.start()
        if self.cfg.keepalive_seconds > 0:
            threading.Thread(target=self._keepalive_loop, daemon=True,
                             name="duplex-keepalive").start()
        if not self._ready.wait(timeout=timeout):
            self._log(f"✗ duplex 会话未在 {timeout:.0f}s 内就绪"
                      f"{'（' + self._error + '）' if self._error else ''}")
            return False
        return True

    def _run_loop(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        except Exception as e:
            self._error = f"{type(e).__name__}: {e}"
            self._log(f"✗ duplex 会话异常：{self._error}")
        finally:
            self._ready.set()
            self._closed.set()
            try:
                loop.close()
            except Exception:
                pass

    async def _main(self):
        import websockets
        headers = {"X-Api-Key": self.cfg.api_key}
        connect_kwargs = dict(ping_interval=None)
        try:
            ws = await websockets.connect(self.cfg.endpoint_url,
                                          additional_headers=headers, **connect_kwargs)
        except TypeError:
            # 兼容 websockets < 14 的参数名
            ws = await websockets.connect(self.cfg.endpoint_url,
                                          extra_headers=headers, **connect_kwargs)
        self._ws = ws
        try:
            logid = None
            try:
                resp = getattr(ws, "response", None)
                hdrs = getattr(resp, "headers", None) or getattr(ws, "response_headers", {})
                logid = hdrs.get("X-Tt-Logid") if hasattr(hdrs, "get") else None
            except Exception:
                pass
            self._log(f"[duplex] 已建连 {self.cfg.endpoint_url}"
                      + (f"（logid={logid}）" if logid else ""))
            await self._send_session_create()
            await self._recv_loop()
        finally:
            try:
                await ws.close()
            except Exception:
                pass
            self._ws = None

    async def _send(self, event: dict):
        if self._ws is None:
            raise RuntimeError("duplex 未连接")
        event.setdefault("event_id", self._next_event_id())
        await self._ws.send(json.dumps(event, ensure_ascii=False, separators=(",", ":")))

    def _next_event_id(self) -> str:
        self._event_id += 1
        return f"event_{self._event_id}"

    async def _send_session_create(self):
        session = {
            "id": self.session_id,
            "model": self.cfg.model,
            "instructions": self.cfg.instructions,
            "audio": {
                "input": {"format": {"type": self.cfg.asr_format, "rate": self.cfg.input_rate}},
                "output": {"format": {"type": self.cfg.tts_format, "rate": self.cfg.output_rate},
                           "voice": self.cfg.voice},
            },
        }
        if self.cfg.enable_tools:
            session["tools"] = []
        extension = {
            "asr": {"extra": {}},
            "tts": {"extra": {}},
            "dialog": {"extra": {"audit_response": self.cfg.audit_response,
                                 "enable_loudness_norm": True}},
        }
        await self._send({"type": T_SESSION_CREATE, "session": session, "extension": extension})

    async def _recv_loop(self):
        while True:
            try:
                frame = await self._ws.recv()
            except Exception as e:
                self._log(f"[duplex] 连接结束：{type(e).__name__}: {e}")
                return
            if isinstance(frame, bytes):
                frame = frame.decode("utf-8", errors="replace")
            try:
                event = json.loads(frame)
            except json.JSONDecodeError:
                continue
            if self._dispatch(event) == "stop":
                return

    def _dispatch(self, event: dict) -> Optional[str]:
        et = event.get("type", "")
        if et == T_SESSION_CREATED:
            self.dialog_id = (event.get("session") or {}).get("id", "")
            self._log(f"[duplex] session.created dialog_id={self.dialog_id}")
            self._ready.set()
        elif et == T_SESSION_CLOSED:
            self._log("[duplex] session.closed")
            return "stop"
        elif et == T_AUDIO_DELTA:
            # 用"当前活跃播报"做关联（见 __init__ 里的说明），拿不到再退回事件里的 id
            sid = self._active_speech_id or event.get("speech_id") or event.get("response_id") or ""
            try:
                pcm = base64.b64decode(event.get("delta") or "")
            except Exception:
                return None
            if sid in self._aborted_speeches:
                self.dropped_bytes += len(pcm)   # 本地打断 → 丢弃，不进播放队列
                return None
            if pcm:
                self.audio_bytes += len(pcm)
                if self._on_audio:
                    try:
                        self._on_audio(pcm)
                    except Exception as e:
                        self._log(f"[duplex] on_audio 回调异常：{e}")
        elif et == T_AUDIO_STARTED:
            self._audio_started.set()
            self._log(f"[duplex] 开始出声 tts_type={event.get('tts_type')}")
        elif et == T_AUDIO_DONE:
            self._audio_done.set()
            self._active_speech_id = None
            self._log(f"[duplex] 出声结束 status={event.get('status_code')} "
                      f"累计音频={self.audio_bytes} 字节"
                      + (f"（打断丢弃 {self.dropped_bytes}）" if self.dropped_bytes else ""))
        elif et == T_TEXT_DELTA:
            self._text_buf.append(event.get("delta") or "")
        elif et == T_TEXT_DONE:
            text = event.get("text") or "".join(self._text_buf)
            self._text_buf.clear()
            if self._on_text_done:
                try:
                    self._on_text_done(text)
                except Exception:
                    pass
        elif et == T_TRANSCRIPT_COMPLETED:
            text = event.get("transcript") or event.get("text") or ""
            self._log(f"[duplex·ASR] {text!r}")
            if text and self._on_transcript:
                try:
                    self._on_transcript(text)
                except Exception:
                    pass
        elif et == T_FUNCTION_CALL:
            self._log(f"[duplex] 收到 function_call（A 模式由本地快脑决策，此处仅记录）")
        elif et == T_CANCELED:
            self._log("[duplex] response.canceled")
        elif et == T_RESPONSE_DONE:
            usage = (event.get("response") or {}).get("usage") or {}
            if usage:
                self.last_usage = usage
                self._log(f"[duplex] response.done usage={usage.get('total_tokens')} tokens")
        elif et == T_ERROR:
            self._error = json.dumps(event.get("error"), ensure_ascii=False)
            self._log(f"✗ duplex 服务端错误：{self._error}")
            self._ready.set()
            return "stop"
        if self._on_event:
            try:
                self._on_event(event)
            except Exception:
                pass
        return None

    def close(self, *, timeout: float = 5.0):
        if self._loop is None or self._closed.is_set():
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(self._send({"type": T_SESSION_CLOSE}),
                                                   self._loop)
            fut.result(timeout=2.0)
        except Exception:
            pass
        self._closed.wait(timeout=timeout)

    # ── 同步 API（任意线程可调）────────────────────────────────────────
    def _call(self, coro_factory, *, timeout: float = 5.0):
        """把协程投递到会话事件循环。

        ⚠️ 参数是**协程工厂而不是协程对象**：若先构造协程再判断"会话是否还活着"，
        会话已关闭时那个协程永远不会被 await → 触发
        `RuntimeWarning: coroutine ... was never awaited`（实测踩过）。
        """
        if self._loop is None or self._closed.is_set() or self._ws is None:
            return None
        try:
            return asyncio.run_coroutine_threadsafe(coro_factory(), self._loop).result(timeout=timeout)
        except Exception as e:
            self._log(f"[duplex] 发送失败：{type(e).__name__}: {e}")
            return None

    def begin_speech(self) -> str:
        """开始一段播报，返回 speech_id。"""
        sid = str(uuid.uuid4())
        self._speech_pending[sid] = None
        # 新一代播报开始 → 重置"说完"信号（同一会话同时只有一段播报）
        self._audio_done.clear()
        self._audio_started.clear()
        return sid

    def wait_speech_done(self, timeout: float = 30.0) -> bool:
        """等这一段播报的音频真正结束（收到 response.output_audio.done）。

        这就是 A 模式里的「音频真正播完才交还发言权」——仲裁器只在 speak() 返回后
        调 `mark_done`，而 speak() 会等到这里返回。
        """
        return self._audio_done.wait(timeout=timeout)

    def append_speech_text(self, speech_id: str, text: str):
        """追加一句（流式）。最后一句留到 commit 时发送，与官方 demo 的 append…commit 形态一致。"""
        prev = self._speech_pending.get(speech_id)
        if prev:
            self._call(lambda: self._send({"type": T_SPEECH_APPEND, "speech_id": speech_id, "text": prev}))
        self._speech_pending[speech_id] = text

    def commit_speech_text(self, speech_id: str, text: str = ""):
        """收尾：把最后一句随 commit 一起发出，触发合成。"""
        last = text or self._speech_pending.get(speech_id) or ""
        self._speech_pending.pop(speech_id, None)
        # 记下"当前活跃播报"，用于把随后到达的音频事件（不带 speech_id）关联回来
        self._active_speech_id = speech_id
        self._call(lambda: self._send({"type": T_SPEECH_COMMIT, "speech_id": speech_id, "text": last}))

    def say(self, text: str) -> str:
        """一次性播报整段文本（前半 append、后半 commit，贴近官方 demo 的流式形态）。"""
        sid = self.begin_speech()
        mid = max(1, len(text) // 2)
        if len(text) > 1:
            self.append_speech_text(sid, text[:mid])
            self.commit_speech_text(sid, text[mid:])
        else:
            self.commit_speech_text(sid, text)
        return sid

    def abort_speech(self, speech_id: str):
        """本地打断：丢弃该 speech 的后续音频。

        注意：duplex 协议在官方 demo 里**没有暴露**"取消一段文本播报"的客户端事件
        （只有服务端下发的 `response.canceled`）。所以这里做的是本地停播，
        服务端仍会把已提交的文本合成完；音频被丢弃、不会再进播放队列。
        """
        if speech_id:
            self._aborted_speeches.add(speech_id)
            self._speech_pending.pop(speech_id, None)
            self._log(f"[duplex] 本地打断 speech={speech_id[:8]}…（后续音频丢弃）")

    def clear_aborted(self, speech_id: str):
        self._aborted_speeches.discard(speech_id)

    def append_mic(self, pcm: bytes):
        """麦克风音频上行（16k / s16le / 单声道）。"""
        if pcm:
            self._last_input_at = time.time()
            self._call(lambda: self._send({"type": T_INPUT_AUDIO_APPEND,
                                           "audio": base64.b64encode(pcm).decode("ascii")}))

    def _keepalive_loop(self):
        """输入心跳：见 `DuplexConfig.keepalive_seconds` 的说明。"""
        k = float(self.cfg.keepalive_seconds)
        while not self._closed.is_set():
            time.sleep(min(max(k / 2.0, 1.0), 5.0))
            if self._closed.is_set() or not self.is_ready:
                continue
            if time.time() - self._last_input_at < k:
                continue
            self.append_mic(b"\x00" * 640)
            self._keepalive_sent += 1
            if self._keepalive_sent == 1:
                self._log(f"[duplex] 已开始输入保活（每 {k:.0f}s 一帧静音）——"
                          f"避免长时间无音频输入被服务端判超时")

    def commit_mic(self):
        self._call(lambda: self._send({"type": T_INPUT_AUDIO_COMMIT}))
    @property
    def is_ready(self) -> bool:
        return self._ready.is_set() and not self._closed.is_set()


# ── 自测 / 试音 CLI ────────────────────────────────────────────────────

def _read_wav_pcm(path: str) -> tuple:
    """读 WAV 返回 (pcm_bytes, rate)；只接受 16-bit 单声道。"""
    with wave.open(path, "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise ValueError(f"{path} 需要 16-bit 单声道（实际 "
                             f"{w.getsampwidth()*8}bit/{w.getnchannels()}ch）")
        return w.readframes(w.getnframes()), w.getframerate()


def _run_input_wav_mode(a, key: str) -> int:
    """把 WAV 当麦克风喂进去 → 验证「收音 + ASR 转写 + 会话原生回答」。

    这条路**不需要 ARK key**：台词与音频都由 duplex 会话自己产出，
    所以在大脑（方舟模型）还没开通时也能先听到声音。
    """
    pcm_in, rate = _read_wav_pcm(a.input_wav)
    transcripts: list = []
    pcm_out = bytearray()

    cfg = DuplexConfig(api_key=key, model=a.model, voice=a.voice, asr_format="pcm")
    sess = DuplexSession(cfg, on_audio=lambda c: pcm_out.extend(c),
                         on_transcript=lambda t: transcripts.append(t))
    print(f"[收音] 输入 {a.input_wav}：{len(pcm_in)} 字节 @{rate}Hz"
          f" ≈ {len(pcm_in)/2/rate:.2f}s")
    if rate != cfg.input_rate:
        print(f"⚠️ 采样率 {rate} != 会话要求的 {cfg.input_rate}，效果可能异常")
    if not sess.start(timeout=20):
        return 1

    # 模拟麦克风：按 640 字节切片流式上行（与官方 demo 的 FILE_CHUNK 一致）
    for i in range(0, len(pcm_in), 640):
        sess.append_mic(pcm_in[i:i + 640])
        time.sleep(0.02)
    sess.commit_mic()
    # 继续送一点静音，帮助服务端确认句尾（官方 demo 的做法）
    for _ in range(50):
        sess.append_mic(b"\x00" * 640)
        time.sleep(0.02)

    # 等回答的音频收完
    t0 = time.time()
    last = 0
    while time.time() - t0 < a.timeout:
        time.sleep(0.2)
        if len(pcm_out) != last:
            last = len(pcm_out)
            t0 = time.time()
        if pcm_out and time.time() - t0 > 1.5:
            break
    sess.close()

    print(f"[收音] ASR 转写：{transcripts or '（未收到转写）'}")
    if not pcm_out:
        print("✗ 没有收到回答音频")
        return 1
    with wave.open(a.out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(cfg.output_rate)
        w.writeframes(bytes(pcm_out))
    print(f"🎉 会话回答 {len(pcm_out)} 字节 ≈ "
          f"{len(pcm_out)/2/cfg.output_rate:.2f} 秒 → {a.out}")
    print("   用播放器打开可以听到「它自己听、自己回答」的效果。")
    return 0


def _cli(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m launcher.duplex_client",
                                description="duplex 试音 / 收音验证（不需要声卡）")
    p.add_argument("--text", default="大家好，我是今晚的主播，先来一句试音。")
    p.add_argument("--input-wav", default="",
                   help="把 WAV 当麦克风喂进去（验证收音+ASR+原生回答；不需要 ARK key）")
    p.add_argument("--out", default="duplex_say.wav")
    p.add_argument("--voice", default=DEFAULT_VOICE)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--key", default=None, help="duplex API Key（默认读环境变量/.env）")
    p.add_argument("--timeout", type=float, default=30.0)
    a = p.parse_args(argv)

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

    key = resolve_api_key(a.key)
    if not key:
        print("✗ 没有 API Key：设 VOLC_DUPLEX_API_KEY（或 VOLC_DIALOG_ACCESS_KEY）"
              "，或用 --key 传入。申请见 docs/CREDENTIALS.md")
        return 1

    if a.input_wav:
        return _run_input_wav_mode(a, key)

    pcm = bytearray()

    def on_audio(chunk: bytes):
        pcm.extend(chunk)

    cfg = DuplexConfig(api_key=key, model=a.model, voice=a.voice)
    sess = DuplexSession(cfg, on_audio=on_audio)
    print(f"[试音] url={cfg.endpoint_url}\n[试音] model={cfg.model} voice={cfg.voice}")
    if not sess.start(timeout=20):
        return 1
    sess.say(a.text)
    # 等音频收完：以"收到音频后静默 1.2s"或超时为准
    t0 = time.time()
    last_len = 0
    while time.time() - t0 < a.timeout:
        time.sleep(0.2)
        if len(pcm) != last_len:
            last_len = len(pcm)
            t0 = time.time()
        if pcm and time.time() - t0 > 1.2:
            break
    sess.close()

    if not pcm:
        print("✗ 没有收到音频")
        return 1
    with wave.open(a.out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(cfg.output_rate)
        w.writeframes(bytes(pcm))
    print(f"🎉 收到 {len(pcm)} 字节 ≈ {len(pcm) / 2 / cfg.output_rate:.2f} 秒 → {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
