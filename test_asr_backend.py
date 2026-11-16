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
    """本機模式（沒有遠端）：Breeze 已停用，一律走 Qwen3-ASR；選單的 qwen／qwen06 決定 1.7B 或 0.6B。"""

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
        t._local_qwen = lambda audio, lang, model="qwen", judge=False: (self.calls.append(model), {"text": "q", "segments": []})[1]

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

    def test_default_is_qwen06_never_whisper(self):
        self._run()
        self.assertEqual(self.calls, ["qwen06"])

    def test_breeze_is_disabled_and_falls_back_to_qwen(self):
        t.ASR_BACKEND_FILE.write_text("breeze")
        self._run()
        self.assertEqual(self.calls, ["qwen06"])
        self.assertNotIn("whisper", self.calls)

    def test_qwen_choice_routes_to_17b_and_back(self):
        t.ASR_BACKEND_FILE.write_text("qwen")
        self._run()
        t.ASR_BACKEND_FILE.write_text("qwen06")
        self._run()
        self.assertEqual(self.calls, ["qwen", "qwen06"])

    def test_remote_host_wins_over_local_qwen(self):
        t.STT_REMOTE_HOST = "m4"
        t.ASR_BACKEND_FILE.write_text("qwen06")
        seen = []
        orig = t._remote_whisper
        t._remote_whisper = lambda *a, **k: (seen.append(1), {"text": "r", "segments": []})[1]
        try:
            self._run()
        finally:
            t._remote_whisper = orig
        self.assertEqual((seen, self.calls), ([1], []))


class ServerLoadsClientBackendsTest(unittest.TestCase):
    """回歸：選單能送的每個後端，遠端伺服器預設都要載入；否則 M2 會收到「沒載入」（曾發生於 qwen06）。"""

    def test_every_menu_backend_is_loaded_by_default(self):
        loaded = {n.strip() for n in asr_server.DEFAULT_LOADED.split(",") if n.strip()}
        self.assertTrue(set(t.ASR_BACKENDS) <= loaded, f"預設只載入 {sorted(loaded)}，缺 {set(t.ASR_BACKENDS) - loaded}")

    def test_every_menu_backend_has_a_builder(self):
        self.assertTrue(set(t.ASR_BACKENDS) <= set(asr_server._builders()))


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


class OctoBackendTest(unittest.TestCase):
    """octo 後端：假引擎，不載模型、不碰 ~/orca/vendor。"""

    def _fake_model(self, text="辨识结果", boom=False):
        seen = {}

        class Fake:
            def generate(self, path, hotwords=None, merge_vad=False):
                import wave
                import numpy as np
                with wave.open(path, "rb") as w:
                    seen.update(rate=w.getframerate(), ch=w.getnchannels(), width=w.getsampwidth(),
                                pcm=np.frombuffer(w.readframes(w.getnframes()), dtype="<i2"))
                seen.update(path=path, hotwords=hotwords, merge_vad=merge_vad)
                if boom:
                    raise RuntimeError("引擎炸了")
                return text
        return Fake(), seen

    def test_writes_16k_mono_wav_and_passes_terms_as_hotwords(self):
        import numpy as np
        model, seen = self._fake_model()
        audio = (np.arange(-800, 800, dtype="float32") / 1000.0)
        out = asr_server._octo_runner(model)(audio, {"terms": ["缸號", "", "PV"], "language": "zh"})
        self.assertEqual(out, {"text": "辨识结果", "segments": []})  # 原文照回，繁體化交給客戶端（跟 qwen 一樣）
        self.assertEqual((seen["rate"], seen["ch"], seen["width"]), (16000, 1, 2))
        self.assertEqual((seen["hotwords"], seen["merge_vad"]), (["缸號", "PV"], True))
        self.assertEqual(len(seen["pcm"]), len(audio))
        np.testing.assert_allclose(seen["pcm"] / 32767.0, np.clip(audio, -1, 1), atol=1e-4)
        self.assertFalse(Path(seen["path"]).exists())  # 暫存檔辨識完就刪

    def test_no_terms_means_no_hotwords_and_dict_result_ok(self):
        import numpy as np

        class Fake:
            def generate(self, path, hotwords=None, merge_vad=False):
                self.hw = hotwords
                return {"text": None}
        m = Fake()
        out = asr_server._octo_runner(m)(np.zeros(160, dtype="float32"), {})
        self.assertEqual((out["text"], m.hw), ("", None))

    def test_temp_file_removed_when_engine_raises(self):
        import numpy as np
        model, seen = self._fake_model(boom=True)
        with self.assertRaises(RuntimeError):
            asr_server._octo_runner(model)(np.zeros(160, dtype="float32"), {})
        self.assertFalse(Path(seen["path"]).exists())

    def test_engine_source_is_imported_lazily_from_VOICE_OCTO_SRC(self):
        import os
        import sys
        with tempfile.TemporaryDirectory() as td:
            src, mdl, vad = (Path(td) / n for n in ("src", "model", "vad"))
            (src / "core").mkdir(parents=True)
            mdl.mkdir()
            vad.mkdir()
            (src / "core" / "__init__.py").write_text("")
            (src / "core" / "auto_model.py").write_text(
                "class AutoModel:\n"
                "    def __init__(self, model, vad_model):\n"
                "        self.args = (model, vad_model)\n"
                "    def generate(self, path, hotwords=None, merge_vad=False):\n"
                "        return 'ok:' + self.args[0] + '|' + self.args[1]\n")
            keep = {k: os.environ.get(k) for k in ("VOICE_OCTO_SRC", "VOICE_OCTO_VAD_REPO")}
            path_before = list(sys.path)
            os.environ.update(VOICE_OCTO_SRC=str(src), VOICE_OCTO_VAD_REPO=str(vad))
            try:
                self.assertNotIn("core.auto_model", sys.modules)  # import asr_server 時沒有載引擎
                run = asr_server._octo_engine(str(mdl))
                self.assertEqual(sys.path[0], str(src))
                import numpy as np
                self.assertEqual(run(np.zeros(160, dtype="float32"), {})["text"], f"ok:{mdl}|{vad}")
            finally:
                sys.path[:] = path_before
                for k in ("core", "core.auto_model"):
                    sys.modules.pop(k, None)
                for k, v in keep.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

    def test_octo_is_a_builder_but_not_loaded_by_default_nor_in_menu(self):
        self.assertIn("octo", asr_server._builders())
        self.assertNotIn("octo", asr_server.DEFAULT_LOADED.split(","))  # ⌘E 的伺服器不多載 1.7B 模型
        self.assertNotIn("octo", t.ASR_BACKENDS)


if __name__ == "__main__":
    unittest.main()
