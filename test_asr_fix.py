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

    def test_account_uses_taiwan_character(self):
        self.assertEqual(t.to_taiwan_traditional("账号和转账"), "帳號和轉帳")

    def test_document_is_not_turned_into_file(self):
        self.assertEqual(t.to_taiwan_traditional("帮我摘要这份长文件，再整理文件"), "幫我摘要這份長文件，再整理文件")
        self.assertEqual(t.to_taiwan_traditional("打开档案"), "開啟檔案")  # 本來就講檔案的不動
        self.assertEqual(t.to_taiwan_traditional("放进文件夹"), "放進資料夾")  # 文件夾→資料夾照舊

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


class AudioJudgeTest(unittest.TestCase):
    def test_candidates_ignore_need_but_respect_block(self):
        self.assertEqual(asr_fix.judge_candidates("麻煩看一下全線"), ["權限"])  # 沒有前後文也要問聲音
        self.assertEqual(asr_fix.judge_candidates("捷運明天全線通車"), [])  # block 詞在＝原詞是對的
        self.assertEqual(asr_fix.judge_candidates("下個月去北美洲"), [])

    def test_candidates_skip_opencc_and_include_term_variants(self):
        rule = mock.Mock(text="Claude", variants=("cloud", "克勞德"))
        self.assertEqual(asr_fix.judge_candidates("請 cloud 看一下許可權", [rule]), ["Claude"])

    def test_accepts_only_when_heard_and_similar(self):
        first = "麻煩看一下全線有沒有開好"
        ok = asr_fix.audio_judge(first, ["權限"], lambda c: "麻煩看一下權限有沒有開好")
        self.assertEqual(ok, "麻煩看一下權限有沒有開好")
        self.assertEqual(asr_fix.audio_judge(first, ["權限"], lambda c: first), first)  # 聲音沒聽出候選
        self.assertEqual(asr_fix.audio_judge(first, ["權限"], lambda c: "權限"), first)  # 照抄熱詞

    def test_rerun_failure_keeps_first_pass(self):
        def boom(_):
            raise RuntimeError("ssh 斷了")
        self.assertEqual(asr_fix.audio_judge("看一下全線", ["權限"], boom), "看一下全線")


class _FakeSession:
    def __init__(self, outputs):
        self.outputs, self.contexts = list(outputs), []

    def transcribe(self, audio, context, language):
        self.contexts.append(context)
        return mock.Mock(text=self.outputs.pop(0))


class JudgeWiringTest(unittest.TestCase):
    def setUp(self):
        self._keep = dict(t._QWEN_SESSION)
        on = mock.patch.object(t, "AUDIO_JUDGE", True)  # 預設關，這裡測開啟時的接線
        on.start()
        self.addCleanup(on.stop)
        p = mock.patch.object(t, "stt_terms", return_value=["A詞", "B詞"])
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(t._QWEN_SESSION.update, self._keep)

    def _run(self, outputs, judge):
        fake = _FakeSession(outputs)
        t._QWEN_SESSION.update(model="qwen06", obj=fake)
        return t._local_qwen(np.zeros(1600, dtype="float32"), "zh", "qwen06", judge=judge)["text"], fake

    def test_local_final_pass_reruns_with_candidates_only(self):
        text, fake = self._run(["麻烦看一下全线有没有开好", "麻烦看一下权限有没有开好"], judge=True)
        self.assertEqual(text, "麻煩看一下權限有沒有開好")
        self.assertEqual(fake.contexts[1], "權限")

    def test_rerun_keeps_terms_already_heard(self):
        rules = [mock.Mock(text="Claude", variants=())]
        with mock.patch.object(t, "load_term_rules", return_value=rules):
            _, fake = self._run(["请 Claude 看一下全线", "请 Claude 看一下权限"], judge=True)
        self.assertEqual(fake.contexts[1], "權限 Claude")

    def test_preview_does_not_rerun(self):
        text, fake = self._run(["麻烦看一下全线有没有开好"], judge=False)
        self.assertEqual(text, "麻煩看一下全線有沒有開好")
        self.assertEqual(len(fake.contexts), 1)

    def test_remote_qwen_reruns_full_segment_with_candidates(self):
        sent = []
        def call(req):
            sent.append(req)
            return {"text": "看一下全线" if len(sent) == 1 else "看一下权限", "backend": "qwen06"}
        with mock.patch.object(t, "current_backend", return_value="qwen06"), \
             mock.patch.object(t, "_remote_call", side_effect=call):
            t._stream_reset()
            out = t._remote_whisper(np.zeros(1600, dtype="float32"), "zh", "p", judge=True)
        self.assertEqual(out["text"], "看一下權限")
        self.assertEqual(sent[1]["terms"], ["權限"])
        self.assertNotIn("op", sent[1])  # 整段請求，不走增量

    def test_remote_breeze_does_not_rerun(self):
        with mock.patch.object(t, "current_backend", return_value="breeze"), \
             mock.patch.object(t, "_remote_call", return_value={"text": "看一下全線", "backend": "breeze"}) as call:
            t._stream_reset()
            t._remote_whisper(np.zeros(1600, dtype="float32"), "zh", "p", judge=True)
        self.assertEqual(call.call_count, 1)


if __name__ == "__main__":
    unittest.main()
