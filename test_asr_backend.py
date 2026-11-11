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


class LoadEnginesTest(unittest.TestCase):
    def test_warm_failure_still_registers(self):
        def breeze():
            def run(a, req):
                raise RuntimeError("暖機炸了")
            return run
        eng = asr_server.load_engines(["breeze"], {"breeze": breeze}, None)
        self.assertEqual(sorted(eng), ["breeze"])  # 舊版：暖機失敗不致命，之後的請求仍可服務

    def test_construction_failure_not_registered(self):
        def qwen():
            raise OSError("權重載不進來")
        eng = asr_server.load_engines(["breeze", "qwen"], {"breeze": lambda: (lambda a, r: {}), "qwen": qwen}, None)
        self.assertEqual(sorted(eng), ["breeze"])

    def test_unknown_backend_skipped(self):
        eng = asr_server.load_engines(["qwen3"], {"breeze": lambda: None}, None)
        self.assertEqual(eng, {})


class ClientRequestTest(unittest.TestCase):
    def setUp(self):
        self._file = t.ASR_BACKEND_FILE
        t.ASR_BACKEND_FILE = Path("/nonexistent/asr-backend")
        self.sent = []
        self._call, self._be = t._remote_call, t.STT_REMOTE_BACKEND
        t._remote_call = lambda req: (self.sent.append(req), {"text": "ok", "segments": [],
                                                                    "backend": req.get("backend", "breeze")})[1]
        t._stream_reset()

    def tearDown(self):
        t._remote_call, t.STT_REMOTE_BACKEND = self._call, self._be
        t.ASR_BACKEND_FILE = self._file

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


class ResponseHandlingTest(unittest.TestCase):
    def setUp(self):
        self._call, self._be, self._file = t._remote_call, t.STT_REMOTE_BACKEND, t.ASR_BACKEND_FILE
        t.ASR_BACKEND_FILE = Path("/nonexistent/asr-backend")
        t._stream_reset()

    def tearDown(self):
        t._remote_call, t.STT_REMOTE_BACKEND, t.ASR_BACKEND_FILE = self._call, self._be, self._file

    def _run(self, resp):
        import numpy as np
        t._remote_call = lambda req: resp
        return t._remote_whisper(np.zeros(1600, dtype="float32"), "zh", "p")

    def test_simplified_output_is_traditionalised_before_term_rules(self):
        from types import SimpleNamespace as R
        t.STT_REMOTE_BACKEND = "qwen"
        out = self._run({"text": "这批订单钢号错误", "segments": [], "backend": "qwen"})
        fixed = t.apply_terms(out["text"], [R(text="缸號", variants=["鋼號"])])
        self.assertEqual(fixed, "這批訂單缸號錯誤")

    def test_backend_mismatch_is_an_error_not_a_silent_swap(self):
        t.STT_REMOTE_BACKEND = "qwen"
        with self.assertRaises(RuntimeError):
            self._run({"text": "x", "segments": [], "backend": "breeze"})
        with self.assertRaises(RuntimeError):
            self._run({"text": "x", "segments": []})  # 舊版伺服器不回報 backend

    def test_no_backend_requested_accepts_any(self):
        t.STT_REMOTE_BACKEND = ""
        self.assertEqual(self._run({"text": "好", "segments": [], "backend": "breeze"})["text"], "好")


class MenuChoiceFileTest(unittest.TestCase):
    def setUp(self):
        self._file, self._be = t.ASR_BACKEND_FILE, t.STT_REMOTE_BACKEND
        self.td = tempfile.TemporaryDirectory()
        t.ASR_BACKEND_FILE = Path(self.td.name) / "asr-backend"

    def tearDown(self):
        t.ASR_BACKEND_FILE, t.STT_REMOTE_BACKEND = self._file, self._be
        self.td.cleanup()

    def test_file_overrides_setting_and_is_reread_each_call(self):
        t.STT_REMOTE_BACKEND = "breeze"
        t.ASR_BACKEND_FILE.write_text("qwen\n")
        self.assertEqual(t.current_backend(), "qwen")
        t.ASR_BACKEND_FILE.write_text("breeze")
        self.assertEqual(t.current_backend(), "breeze")  # 不用重啟

    def test_missing_or_unknown_falls_back_to_setting(self):
        t.STT_REMOTE_BACKEND = "qwen"
        self.assertEqual(t.current_backend(), "qwen")  # 檔案不存在
        t.ASR_BACKEND_FILE.write_text("whisper-xl")
        self.assertEqual(t.current_backend(), "qwen")  # 不認得
        t.STT_REMOTE_BACKEND = ""
        self.assertEqual(t.current_backend(), "")


class LocalRoutingTest(unittest.TestCase):
    """本機模式（沒有遠端）：選 qwen 才走 Qwen；沒選或選 breeze 必須維持原本的 mlx_whisper。"""

    def setUp(self):
        import sys
        import types
        self._keep = (t.ASR_BACKEND_FILE, t.STT_REMOTE_BACKEND, t.STT_REMOTE_HOST, t._local_qwen,
                      sys.modules.get("mlx_whisper"))
        self.td = tempfile.TemporaryDirectory()
        t.ASR_BACKEND_FILE = Path(self.td.name) / "asr-backend"
        t.STT_REMOTE_BACKEND, t.STT_REMOTE_HOST = "", ""
        self.calls = []
        fake = types.ModuleType("mlx_whisper")
        fake.transcribe = lambda *a, **k: (self.calls.append("whisper"), {"text": "w", "segments": []})[1]
        sys.modules["mlx_whisper"] = fake
        t._local_qwen = lambda audio, lang: (self.calls.append("qwen"), {"text": "q", "segments": []})[1]

    def tearDown(self):
        import sys
        t.ASR_BACKEND_FILE, t.STT_REMOTE_BACKEND, t.STT_REMOTE_HOST, t._local_qwen, mod = self._keep
        if mod is None:
            sys.modules.pop("mlx_whisper", None)
        else:
            sys.modules["mlx_whisper"] = mod
        self.td.cleanup()

    def _run(self):
        import numpy as np
        return t._whisper("zh", audio=np.zeros(1600, dtype="float32"))

    def test_default_is_unchanged_whisper(self):
        self._run()
        self.assertEqual(self.calls, ["whisper"])

    def test_breeze_choice_is_whisper(self):
        t.ASR_BACKEND_FILE.write_text("breeze")
        self._run()
        self.assertEqual(self.calls, ["whisper"])

    def test_qwen_choice_routes_to_qwen_and_switches_back_without_restart(self):
        t.ASR_BACKEND_FILE.write_text("qwen")
        self._run()
        t.ASR_BACKEND_FILE.write_text("breeze")
        self._run()
        self.assertEqual(self.calls, ["qwen", "whisper"])

    def test_remote_host_wins_over_local_qwen(self):
        t.STT_REMOTE_HOST = "m4"
        t.ASR_BACKEND_FILE.write_text("qwen")
        seen = []
        orig = t._remote_whisper
        t._remote_whisper = lambda *a, **k: (seen.append(1), {"text": "r", "segments": []})[1]
        try:
            self._run()
        finally:
            t._remote_whisper = orig
        self.assertEqual((seen, self.calls), ([1], []))


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
