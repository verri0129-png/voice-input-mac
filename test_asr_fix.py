"""asr_fix：假字過濾與看前後文的修字表（與櫃檯小妹同一套規則），以及它們接進 transcribe 的位置。"""
import unittest
from unittest import mock

import numpy as np

import asr_fix
import transcribe as t


class FixContextTest(unittest.TestCase):
    def test_opencc_mistranslation_is_undone_on_every_path(self):
        # 真的 OpenCC s2twp：权限 → 許可權，to_taiwan_traditional 收尾要改回權限
        self.assertEqual(t.to_taiwan_traditional("检查一下权限有没有开好"), "檢查一下權限有沒有開好")

    def test_needs_context_word(self):
        self.assertEqual(asr_fix.fix_context("帳號的全線要設定"), "帳號的權限要設定")
        self.assertEqual(asr_fix.fix_context("麻煩看一下全線"), "麻煩看一下全線")

    def test_block_word_wins(self):
        self.assertEqual(asr_fix.fix_context("捷運明天全線通車，記得設定鬧鐘"), "捷運明天全線通車，記得設定鬧鐘")
        self.assertEqual(asr_fix.fix_context("下個月去美洲出差，週報先交"), "下個月去美洲出差，週報先交")

    def test_case_insensitive_and_idempotent(self):
        once = asr_fix.fix_context("把 chrome 的排程改掉")
        self.assertEqual(once, "把 cron 的排程改掉")
        self.assertEqual(asr_fix.fix_context(once), once)


class DropPhantomTest(unittest.TestCase):
    def test_only_phantom_is_dropped(self):
        self.assertEqual(asr_fix.drop_phantom("謝謝收看。"), "")
        self.assertEqual(asr_fix.drop_phantom("Thanks for watching!"), "")

    def test_real_request_mentioning_phantom_stays(self):
        self.assertEqual(asr_fix.drop_phantom("幫我找有中文字幕的電影"), "幫我找有中文字幕的電影")

    def test_quiet_only_needs_quiet_audio(self):
        self.assertEqual(asr_fix.drop_phantom("謝謝。", quiet=True), "")
        self.assertEqual(asr_fix.drop_phantom("謝謝。", quiet=False), "謝謝。")
        self.assertEqual(asr_fix.drop_phantom("謝謝。"), "謝謝。")  # 音量不明 → 不刪
        self.assertEqual(asr_fix.drop_phantom("謝謝你幫忙", quiet=True), "謝謝你幫忙")


class WiringTest(unittest.TestCase):
    def test_quiet_level_is_judged_from_audio(self):
        loud = (np.sin(np.arange(16000) / 5) * 0.3).astype("float32")  # 約 -13 dBFS
        quiet = loud * 0.05  # 約 -39 dBFS
        self.assertEqual(t._drop_phantom("好", loud), "好")
        self.assertEqual(t._drop_phantom("好", quiet), "")

    def test_transcribe_audio_drops_phantom(self):
        speech = (np.sin(np.arange(32000) / 5) * np.linspace(0, 0.5, 32000)).astype("float32")
        with mock.patch.object(t, "is_silence", return_value=False), \
             mock.patch.object(t, "_whisper", return_value={"text": "謝謝觀看", "segments": []}):
            self.assertEqual(t.transcribe_audio(speech), "")


if __name__ == "__main__":
    unittest.main()
