"""duplex 语音层回归测试（真连服务端）。

**需要 API Key，否则整类跳过** —— 所以它在本机（有 Key）会真跑，
在没配 Key 的 CI 上自动跳过，不会把 CI 弄红。

它验证的核心是**发声器与协议的时序**（这正是 `lumi_tts.speak()` 依赖的东西）：
    feed(句1) → feed(句2) → finish()
    ⇒ 服务端应把**两句都念出来**（append 中间句 + commit 末句）
    ⇒ 收到音频、且音频真的被播放器消费掉

不需要声卡：音频落盘（`wav_dir`）；这正是无声环境下的验收手段。
"""
from __future__ import annotations

import os
import shutil
import time
import unittest
from pathlib import Path

from launcher.duplex_client import DEFAULT_VOICE, resolve_api_key

# ⚠️ 音频输出放在**工作区内**（logs/ 已被 .gitignore 覆盖），
#    不用 tempfile：受限沙箱不允许写系统临时目录（实测 PermissionError）。
AUDIO_DIR = Path("logs/duplex_test_audio")


def _has_key() -> bool:
    if resolve_api_key():
        return True
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    return bool(resolve_api_key())


@unittest.skipUnless(_has_key(), "需要 duplex API Key（VOLC_DUPLEX_API_KEY / VOLC_DIALOG_ACCESS_KEY）")
class DuplexVoiceTests(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(AUDIO_DIR, ignore_errors=True)
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        self.logs = []
        from launcher.duplex_voice import DuplexVoice
        self.voice = DuplexVoice(
            api_key=resolve_api_key(),
            characters=["fames"],
            log_fn=lambda m: self.logs.append(str(m)),
            pa=None,                       # 无声卡 → 音频落盘
            cable_indices={},
            voice_of=lambda _sp: DEFAULT_VOICE,
            wav_dir=str(AUDIO_DIR),
        )
        if not self.voice.start():
            self.skipTest("duplex 会话未能就绪（凭证/网络问题）")

    def tearDown(self):
        try:
            self.voice.close()
        except Exception:
            pass

    def test_emitter_speaks_all_sentences(self):
        """两句话都要被念出来，且音频被真正消费（验证 append/commit 时序）。"""
        em = self.voice.make_emitter("fames")
        self.assertIsNotNone(em, "应能为角色取到发声器")

        em.feed("第一句，这是一次流式播报测试。")
        self.assertIsNotNone(em.stream_task_id, "首句 feed 后应有发声流 id")
        em.feed("第二句，如果两句话都被念出来，说明时序是对的。")
        em.finish()

        player = self.voice.players["fames"]
        self.assertGreater(player.bytes_in, 8000,
                           f"应收到音频（实际 {player.bytes_in} 字节）")
        self.assertGreater(player.bytes_played, 0, "音频应被播放器消费")
        self.assertLessEqual(player.bytes_played, player.bytes_in + 1)
        # 播完时长（按真实音频字节数）——比"文本长度估算"可靠，可喂给可观测性
        self.assertGreater(self.voice.speech_seconds("fames"), 0.5)

    def test_second_turn_reuses_session(self):
        """同一会话可以连续播报多轮（引擎里每轮 speak() 都会新建发声器）。"""
        first = self.voice.say("fames", "第一轮，先打个招呼。")
        self.assertTrue(first, "第一轮应正常播完")
        n1 = self.voice.players["fames"].bytes_in
        second = self.voice.say("fames", "第二轮，再来一句。")
        self.assertTrue(second, "第二轮应正常播完")
        self.assertGreater(self.voice.players["fames"].bytes_in, n1,
                           "第二轮应继续产生音频")

    def test_abort_drops_further_audio(self):
        """打断后**不应再有新音频进入播放队列**（本地丢弃语义）。

        注意断言的语义边界：打断前已经进入队列/正在写声卡的那一点音频无法收回，
        所以这里校验的是"打断之后不再有新的音频进入"（bytes_in 冻结）+ 服务端确实
        送来了被丢弃的字节数（dropped_bytes > 0，证明过滤真的生效而不是碰巧没数据）。
        """
        em = self.voice.make_emitter("fames")
        em.feed("这是一句会被打断的长话，本来应该念很久很久，用来验证打断是否生效。")
        em.feed("还有第二句，同样不应该被念完，所以这里需要足够多的文本。")
        # 触发合成（但不走 finish，否则会阻塞到播完）→ 服务端开始送音频后再打断
        self.voice.sessions["fames"].commit_speech_text(em.stream_task_id, "")
        time.sleep(0.6)
        player = self.voice.players["fames"]
        em.abort()
        frozen = player.bytes_in
        time.sleep(1.5)

        self.assertEqual(player.bytes_in, frozen,
                         "打断后不应再有新音频进入播放队列")
        self.assertGreater(self.voice.sessions["fames"].dropped_bytes, 0,
                           "服务端应仍在送音频，且被我们丢弃（否则说明过滤没生效）")


if __name__ == "__main__":
    unittest.main()
