import unittest

import transcribe

RULES = transcribe.load_term_rules("rewrite_terms.json")


class TermsSurviveTraditionalTests(unittest.TestCase):
    """OpenCC s2twp 會把「脚本／腳本」轉成「指令碼」；詞庫的「指令碼→腳本」必須留住。"""

    def test_to_taiwan_still_maps_script_phrase(self):
        # 前提：這是造成問題的原因；若 OpenCC 行為變了，這裡會先紅
        self.assertEqual(transcribe.to_taiwan_traditional("腳本"), "指令碼")

    def test_postprocess_keeps_script_term(self):
        out = transcribe.postprocess_transcript("不要再寫指令碼了", rules=RULES)
        self.assertIn("腳本", out["text"])
        self.assertNotIn("指令碼", out["text"])

    def test_settle_text_keeps_script_term(self):
        out = transcribe._settle_text("不要再寫指令碼了", RULES)
        self.assertIn("腳本", out)
        self.assertNotIn("指令碼", out)


if __name__ == "__main__":
    unittest.main()
