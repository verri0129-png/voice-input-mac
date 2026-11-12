import tempfile
import unittest
from pathlib import Path

import rewrite_local
import transcribe
from rewrite_cloud import RewriteRejected


class LocalRewriteTests(unittest.TestCase):
    def setUp(self):
        self._orig = rewrite_local.generate_text

    def tearDown(self):
        rewrite_local.generate_text = self._orig

    def _stub(self, output):
        rewrite_local.generate_text = lambda *args, **kwargs: output

    def test_punctuation_only_change_is_accepted(self):
        self._stub("所以我假如看到錯字沒有被改過來，就代表那個Gemini它呼叫不到對不對？")
        text, reason, changed = rewrite_local.local_rewrite(
            "所以我假如看到錯字沒有被改過來就代表那個Gemini它呼叫不到對不對"
        )
        self.assertIn("呼叫不到對不對", text)
        self.assertIn("，", text)

    def test_content_change_is_rejected(self):
        # 模型自己加了「一起」——守門必須擋下，不能讓它改內容
        self._stub("或者是一起有論事，但是由誰來做。")
        with self.assertRaises(RewriteRejected):
            rewrite_local.local_rewrite("或者是有論事但是由誰來做")

    def test_answering_the_question_is_rejected(self):
        # 小模型常見毛病：把問句當成要回答的問題
        self._stub("它沒有潤飾功能。")
        with self.assertRaises(RewriteRejected):
            rewrite_local.local_rewrite("你先告訴我這個模型有沒有潤飾的功能")

    def test_empty_output_is_rejected(self):
        self._stub("   ")
        with self.assertRaises(RewriteRejected):
            rewrite_local.local_rewrite("測試一下")

    def test_apply_rewrite_local_qwen_uses_model_output(self):
        self._stub("你能不能把我的 voice input mac，我不知道裡面有沒有 log file。")
        with tempfile.TemporaryDirectory() as tmp:
            settings = transcribe.rewrite_settings(
                backend="local-qwen", terms_file=str(Path(tmp) / "none.json")
            )
            text, info = transcribe.apply_rewrite(
                "你能不能把我的voice input mac我不知道裡面有沒有log file",
                config=settings,
            )
        self.assertEqual(info["backend"], "local-qwen")
        self.assertIn("log file", text)

    def test_apply_rewrite_local_qwen_falls_back_to_rules_on_rejection(self):
        self._stub("它沒有潤飾功能。")
        with tempfile.TemporaryDirectory() as tmp:
            settings = transcribe.rewrite_settings(
                backend="local-qwen", terms_file=str(Path(tmp) / "none.json")
            )
            text, info = transcribe.apply_rewrite(
                "你先告訴我這個模型有沒有潤飾的功能",
                config=settings,
            )
        self.assertEqual(info["backend"], "rules-fallback")
        self.assertNotIn("它沒有潤飾功能", text)

    def test_apply_rewrite_local_qwen_applies_term_dictionary(self):
        # 模型沒照詞庫寫（quen），詞庫替換必須在最後補上
        self._stub("repo千問quen。")
        with tempfile.TemporaryDirectory() as tmp:
            terms = Path(tmp) / "terms.json"
            terms.write_text(
                '{"version": 1, "terms": [{"text": "Qwen", "match": ["quen"]}]}',
                encoding="utf-8",
            )
            settings = transcribe.rewrite_settings(backend="local-qwen", terms_file=str(terms))
            text, info = transcribe.apply_rewrite("repo千問quen", config=settings)
        self.assertEqual(info["backend"], "local-qwen")
        self.assertIn("Qwen", text)
        self.assertNotIn("quen", text)


if __name__ == "__main__":
    unittest.main()
