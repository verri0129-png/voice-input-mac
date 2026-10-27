"""標點相關的回歸測試：問號判斷、只搬標點、守門拒絕後的降級。

2026-10-27 起因：M2 實跑 121 次聽寫中，定版有 65 次放棄（56 次 timeout、7 次守門誤殺），
使用者看到的永遠是沒有標點的規則版。這裡釘住三件事：
1. 疑問詞（什麼/怎麼/為什麼…）要拿到問號，但前面有否定時不要。
2. 守門拒絕雲端結果時，要退成「只搬標點」，字一個都不能動。
3. 這兩條都不能反過來改到原話。
"""

import unittest

import transcribe
import rewrite_cloud


class TerminalMarkTests(unittest.TestCase):
    def test_existing_tail_questions(self):
        for text in ("這樣對不對", "你吃了嗎", "可以嗎", "是吧"):
            self.assertEqual(transcribe._terminal_mark(text), "？", text)

    def test_wh_questions_get_question_mark(self):
        cases = (
            "好啦那我現在那個語音轉文字標點符號的問題要怎麼解",
            "什麼時候才可以開始用純語音控制電腦啊",
            "你建議哪一個啦是要壓縮還是開心的session",
            "這是什麼意思",
            "到底要多久",
        )
        for text in cases:
            self.assertEqual(transcribe._terminal_mark(text), "？", text)

    def test_negated_wh_stays_statement(self):
        cases = (
            "我不知道他為什麼沒來",
            "我沒有說誰要來",
            "沒人告訴我怎麼做",
        )
        for text in cases:
            self.assertEqual(transcribe._terminal_mark(text), "。", text)

    def test_statement_stays_period(self):
        for text in ("今天天氣很好", "我先去開會"):
            self.assertEqual(transcribe._terminal_mark(text), "。", text)

    def test_finalize_adds_question_mark(self):
        self.assertTrue(
            transcribe.finalize_punctuation("這個問題要怎麼解").endswith("？")
        )
        self.assertTrue(
            transcribe.finalize_punctuation("我今天下午要開會").endswith("。")
        )


class TransferPunctuationTests(unittest.TestCase):
    def test_only_punctuation_moves(self):
        raw = "好啦那我現在那個語音轉文字標點符號的問題要怎麼解"
        polished = "那我現在那個語音轉文字標點符號的問題，要怎麼解？"
        out = transcribe.transfer_punctuation(raw, polished)
        # 字一個都不能少、不能改（模型刪掉的「好啦」要留著）
        self.assertIn("好啦", out)
        self.assertEqual(
            transcribe._text_only(out), transcribe._text_only(raw), out
        )
        self.assertIn("，", out)


class RejectedRewriteFallbackTests(unittest.TestCase):
    def setUp(self):
        self._orig = transcribe.rewrite_draft

    def tearDown(self):
        transcribe.rewrite_draft = self._orig

    def test_rejection_falls_back_to_punctuation_only(self):
        def fake_rewrite(text, **kwargs):
            raise rewrite_cloud.RewriteRejected(
                "雲端刪掉了非贅字「好啦」",
                model_text="那我現在那個語音轉文字標點符號的問題，要怎麼解？",
            )

        transcribe.rewrite_draft = fake_rewrite
        out, info = transcribe.apply_rewrite(
            "好啦那我現在那個語音轉文字標點符號的問題要怎麼解",
            enabled=True,
            backend="cloud",
            config=transcribe.rewrite_settings(backend="cloud"),
        )
        self.assertEqual(info.get("backend"), "cloud:punct-only", info)
        self.assertIn("好啦", out)
        self.assertIn("，", out)
        self.assertTrue(out.rstrip().endswith(("？", "。")), out)

    def test_rejection_without_model_text_keeps_rules(self):
        def fake_rewrite(text, **kwargs):
            raise rewrite_cloud.RewriteRejected("雲端刪掉了非贅字「好啦」", model_text="")

        transcribe.rewrite_draft = fake_rewrite
        out, info = transcribe.apply_rewrite(
            "好啦那我現在那個問題要怎麼解",
            enabled=True,
            backend="cloud",
            config=transcribe.rewrite_settings(backend="cloud"),
        )
        self.assertEqual(info.get("backend"), "rules-fallback", info)
        self.assertEqual(transcribe._text_only(out), transcribe._text_only("好啦那我現在那個問題要怎麼解"))


if __name__ == "__main__":
    unittest.main()
