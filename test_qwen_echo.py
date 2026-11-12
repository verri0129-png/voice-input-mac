import unittest
from types import SimpleNamespace

import transcribe

ECHO = "詞庫 Claude Code 缸號 補裁 燙縮 大貨 抱怨函"


class _FakeSession:
    """依 context 回不同結果：有熱詞時照抄，無熱詞時給正常句子。"""

    def __init__(self, echo_always=False):
        self.calls = []
        self.echo_always = echo_always

    def transcribe(self, audio, context="", language=None):
        self.calls.append(context)
        if self.echo_always or context:
            return SimpleNamespace(text=ECHO)
        return SimpleNamespace(text="這個缸號是對的。")


class QwenEchoTests(unittest.TestCase):
    def setUp(self):
        self._orig = transcribe._qwen_session

    def tearDown(self):
        transcribe._qwen_session = self._orig

    def _use(self, fake):
        transcribe._qwen_session = lambda model: fake

    def test_echo_triggers_retry_without_context(self):
        fake = _FakeSession()
        self._use(fake)
        out = transcribe._local_qwen(None, "zh")
        self.assertEqual(out["text"], "這個缸號是對的。")
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(fake.calls[1], "")

    def test_echo_twice_is_dropped(self):
        fake = _FakeSession(echo_always=True)
        self._use(fake)
        out = transcribe._local_qwen(None, "zh")
        self.assertEqual(out["text"], "")

    def test_normal_output_is_untouched(self):
        fake = SimpleNamespace(
            transcribe=lambda audio, context="", language=None: SimpleNamespace(text="我們的補裁庫存要盤點。"),
            calls=[],
        )
        self._use(fake)
        out = transcribe._local_qwen(None, "zh")
        self.assertEqual(out["text"], "我們的補裁庫存要盤點。")

    def test_simplified_echo_is_caught(self):
        # Qwen 實際吐的是簡體（词库），比對前要繁化
        class _Simp(_FakeSession):
            def transcribe(self, audio, context="", language=None):
                self.calls.append(context)
                if context:
                    return SimpleNamespace(text="词库 Claude Code 缸號 補裁 燙縮")
                return SimpleNamespace(text="這個缸號是對的。")

        self._use(_Simp())
        self.assertEqual(transcribe._local_qwen(None, "zh")["text"], "這個缸號是對的。")

    def test_echo_detector_ignores_spaces(self):
        terms = ["詞庫", "Claude Code", "缸號", "補裁"]
        self.assertTrue(transcribe._qwen_echoes_context("詞庫 ClaudeCode 缸號 補裁。", terms))
        self.assertFalse(transcribe._qwen_echoes_context("詞庫是什麼", terms))

    def test_mid_list_echo_is_caught(self):
        # 0.6B 實測：從清單中間開始照抄（KNT551 起頭），只比開頭抓不到
        terms = ["詞庫", "Claude Code", "缸號", "補裁", "KNT551", "FEPO", "MQP"]
        self.assertTrue(transcribe._qwen_echoes_context("KNT551 FEPO MQP 缸號 補裁 詞庫", terms))
        self.assertFalse(transcribe._qwen_echoes_context("這個缸號的 KNT551 要確認", terms))


if __name__ == "__main__":
    unittest.main()
