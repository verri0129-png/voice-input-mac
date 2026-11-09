"""遠端 ASR 的後端選擇（breeze／qwen）：asr_server.run_backend 與 transcribe 的請求內容。不載模型。"""
import json
import tempfile
import unittest
from pathlib import Path

import asr_server
import transcribe as t


class RunBackendTest(unittest.TestCase):
    def setUp(self):
        self.engines = {
            "breeze": lambda a, req: {"text": "B", "segments": []},
            "qwen": lambda a, req: {"text": "Q:" + " ".join(req.get("terms") or []), "segments": []},
        }

    def test_request_backend_wins(self):
        out = asr_server.run_backend(self.engines, "breeze", {"backend": "qwen", "terms": ["缸號", "PV"]}, None)
        self.assertEqual((out["text"], out["backend"]), ("Q:缸號 PV", "qwen"))

    def test_default_when_unspecified(self):
        self.assertEqual(asr_server.run_backend(self.engines, "breeze", {}, None)["backend"], "breeze")

    def test_unloaded_backend_is_an_error_not_a_silent_swap(self):
        out = asr_server.run_backend({"breeze": self.engines["breeze"]}, "breeze", {"backend": "qwen"}, None)
        self.assertIn("error", out)
        self.assertNotIn("text", out)


class ClientRequestTest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self._call, self._be = t._remote_call, t.STT_REMOTE_BACKEND
        t._remote_call = lambda req: (self.sent.append(req), {"text": "ok", "segments": []})[1]
        t._stream_reset()

    def tearDown(self):
        t._remote_call, t.STT_REMOTE_BACKEND = self._call, self._be

    def test_backend_and_terms_sent_only_when_selected(self):
        import numpy as np
        a = np.zeros(1600, dtype="float32")
        t.STT_REMOTE_BACKEND = ""
        t._remote_whisper(a, "zh", "p")
        self.assertNotIn("backend", self.sent[-1])
        self.assertNotIn("terms", self.sent[-1])
        t.STT_REMOTE_BACKEND = "qwen"
        t._remote_whisper(a, "zh", "p")
        self.assertEqual(self.sent[-1]["backend"], "qwen")
        self.assertIn("terms", self.sent[-1])


class SttTermsTest(unittest.TestCase):
    def test_skips_sentence_fragments_and_caps(self):
        d = {"version": 1, "terms": [{"text": x} for x in
             ["缸號", "不要測啊，", "Claude Code", "還是soniox（有", "x" * 20, "KNT551"]]}
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "terms.json"
            p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(t.stt_terms(str(p)), ["缸號", "Claude Code", "KNT551"])

    def test_missing_file_is_empty(self):
        self.assertEqual(t.stt_terms("/nonexistent/terms.json"), [])


if __name__ == "__main__":
    unittest.main()
