"""「先等潤稿再貼上」：關（預設）＝先貼規則版、背景定版（原行為）；
開＝最多等 N 秒，等到就直接貼潤稿版，等不到就退回原行為。

不碰網路、不碰模型：辨識與背景潤稿都換成假的。
"""
import contextlib
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import transcribe
from rewrite_cloud import RewriteConfig

RAW = "我們明天開會討論預算"
REFINED = "我們明天開會，討論預算。"


def _fake_refiner(delay, payload):
    """模擬 refine_in_background：睡 delay 秒後把 payload 寫進 refine 檔。"""
    def run(raw, context, settings, refine_path, pasted_text):
        time.sleep(delay)
        transcribe._write_json_atomic(refine_path, dict(payload))
    return run


class WaitSecondsTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.file = Path(self.td.name) / "wait-refine"
        self._saved = (transcribe.WAIT_REFINE_FILE, transcribe.WAIT_REFINE_ENABLED,
                       transcribe.WAIT_REFINE_MAX)
        transcribe.WAIT_REFINE_FILE = self.file
        transcribe.WAIT_REFINE_ENABLED = False
        transcribe.WAIT_REFINE_MAX = 1.5

    def tearDown(self):
        (transcribe.WAIT_REFINE_FILE, transcribe.WAIT_REFINE_ENABLED,
         transcribe.WAIT_REFINE_MAX) = self._saved
        self.td.cleanup()

    def test_default_is_off(self):
        self.assertEqual(transcribe.wait_refine_seconds(), 0.0)

    def test_env_on_uses_env_max(self):
        transcribe.WAIT_REFINE_ENABLED = True
        self.assertEqual(transcribe.wait_refine_seconds(), 1.5)
        transcribe.WAIT_REFINE_MAX = 2.5
        self.assertEqual(transcribe.wait_refine_seconds(), 2.5)

    def test_menu_file_overrides_env(self):
        transcribe.WAIT_REFINE_ENABLED = True
        self.file.write_text("off\n", encoding="utf-8")
        self.assertEqual(transcribe.wait_refine_seconds(), 0.0)
        self.file.write_text("0", encoding="utf-8")
        self.assertEqual(transcribe.wait_refine_seconds(), 0.0)
        transcribe.WAIT_REFINE_ENABLED = False
        self.file.write_text("2", encoding="utf-8")
        self.assertEqual(transcribe.wait_refine_seconds(), 2.0)
        self.file.write_text("on", encoding="utf-8")
        self.assertEqual(transcribe.wait_refine_seconds(), 1.5)

    def test_garbage_file_falls_back_to_env(self):
        self.file.write_text("banana", encoding="utf-8")
        self.assertEqual(transcribe.wait_refine_seconds(), 0.0)
        transcribe.WAIT_REFINE_ENABLED = True
        self.assertEqual(transcribe.wait_refine_seconds(), 1.5)

    def test_wait_is_capped(self):
        # 等太久會讓 ⌘E 看起來像壞掉；上限擋住手滑設成 60。
        self.file.write_text("60", encoding="utf-8")
        self.assertLessEqual(transcribe.wait_refine_seconds(), transcribe.WAIT_REFINE_CAP)


class ProcessWavWaitTests(unittest.TestCase):
    """process_wav_file 在 refine_path 給定（⌘E 路徑）時的兩種行為。"""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.refine = str(Path(self.td.name) / "refine.json")
        self.settings = RewriteConfig(backend="cloud", project="p", location="global",
                                      terms_file=str(Path(self.td.name) / "no-terms.json"))
        patches = [
            mock.patch.object(transcribe, "STT_PROVIDER", "local"),
            mock.patch.object(transcribe, "SECOND_OPINION", False),
            mock.patch.object(transcribe, "wav_samples", lambda path: None),
            mock.patch.object(transcribe, "_whisper", lambda *a, **k: {"text": RAW}),
            mock.patch.object(transcribe, "suspect_reason", lambda *a, **k: []),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        # 背景潤稿執行緒可能還在睡；等它寫完再刪暫存目錄
        for t in threading.enumerate():
            if t.name == "voice-refine":
                t.join(3)
        self.td.cleanup()

    def _run(self, wait, refiner):
        err = io.StringIO()
        with mock.patch.object(transcribe, "wait_refine_seconds", lambda: wait), \
             mock.patch.object(transcribe, "refine_in_background", refiner), \
             contextlib.redirect_stderr(err):
            started = time.perf_counter()
            out = transcribe.process_wav_file(
                "/tmp/x.wav", config=self.settings, refine_path=self.refine
            )
            elapsed = time.perf_counter() - started
        return out, elapsed, err.getvalue()

    def test_off_pastes_rules_version_without_waiting(self):
        out, elapsed, log = self._run(
            0.0, _fake_refiner(0.4, {"ok": True, "text": REFINED}))
        self.assertEqual(out["status"], "ok")
        self.assertTrue(out["refine_pending"])
        self.assertNotEqual(out["text"], REFINED)
        self.assertLess(elapsed, 0.3)
        self.assertNotIn("[wait-refine]", log)

    def test_on_and_refine_in_time_pastes_refined(self):
        out, elapsed, log = self._run(
            1.5, _fake_refiner(0.05, {"ok": True, "text": REFINED, "backend": "local-brain"}))
        self.assertEqual(out["text"], REFINED)
        self.assertFalse(out.get("refine_pending", False))
        self.assertEqual(out["rewrite"]["backend"], "refined-before-paste")
        self.assertLess(elapsed, 1.0)
        self.assertRegex(log, r"\[wait-refine\] 等到潤稿 \d+ms（上限 1\.5s）→ 貼潤稿版")

    def test_on_but_too_slow_pastes_rules_and_keeps_background(self):
        out, elapsed, log = self._run(
            0.2, _fake_refiner(0.8, {"ok": True, "text": REFINED}))
        self.assertNotEqual(out["text"], REFINED)
        self.assertTrue(out["refine_pending"])
        self.assertLess(elapsed, 0.6)
        self.assertRegex(log, r"\[wait-refine\] 等 \d+ms 沒等到（上限 0\.2s）→ 先貼規則版")
        # 背景照舊：稍後 refine 檔仍會寫出來給 App 原地定版
        self.assertEqual(transcribe.read_refined_text(self.refine, 2.0), REFINED)

    def test_on_and_refine_gives_up_pastes_rules_without_pending(self):
        out, _, log = self._run(
            1.5, _fake_refiner(0.05, {"ok": False, "reason": "雲端回空字串"}))
        self.assertNotEqual(out["text"], REFINED)
        self.assertFalse(out.get("refine_pending", False))
        self.assertIn("[wait-refine] 潤稿放棄", log)
        self.assertIn("→ 貼規則版", log)

    def test_on_refined_text_is_still_settled(self):
        # 直接貼的潤稿版也要經過繁化：簡體不能漏出去
        out, _, _ = self._run(
            1.5, _fake_refiner(0.0, {"ok": True, "text": "我们明天开会。"}))
        self.assertEqual(out["text"], "我們明天開會。")


if __name__ == "__main__":
    unittest.main()
