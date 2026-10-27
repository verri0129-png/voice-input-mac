"""第二意見（Vertex 轉錄模型）的純邏輯測試：不碰網路、不碰 ADC。

守門原則跟 test_rewrite_cloud 一樣：會被使用者看到的東西（貼上的字）這裡一行都不動，
所以測試只驗「請求長什麼樣、回應怎麼解析、比對怎麼算、記錄怎麼統計」。
"""
import base64
import json
import tempfile
import unittest
from pathlib import Path

import transcribe
from rewrite_cloud import RewriteConfig


SETTINGS = RewriteConfig(project="project-test", location="global")


def _response(text, *, style="audioTranscription", prompt_tokens=210, out_tokens=28):
    part = {}
    if style == "audioTranscription":
        part["audioTranscription"] = {"text": text}
        part["text"] = text
    else:
        part["text"] = text
    return {
        "candidates": [{"content": {"role": "model", "parts": [part]}}],
        "usageMetadata": {
            "promptTokenCount": prompt_tokens,
            "candidatesTokenCount": out_tokens,
        },
    }


class CompareTests(unittest.TestCase):
    def test_identical_texts_agree(self):
        out = transcribe.compare_transcripts("我們明天早上十點開會", "我們明天早上十點開會。")
        self.assertTrue(out["agree"])
        self.assertEqual(out["spans"], [])
        self.assertEqual(out["diff_chars"], 0)

    def test_simplified_cloud_matches_traditional_local(self):
        # 雲端回簡體、本地是繁體：繁化後應該算「一致」，不然每一句都會被記成錯。
        out = transcribe.compare_transcripts(
            "請準備好上週的布料檢驗報告", "请准备好上周的布料检验报告"
        )
        self.assertTrue(out["agree"], out)

    def test_misheard_char_becomes_a_span(self):
        out = transcribe.compare_transcripts(
            "另外裁剪端地問題要先確認", "另外裁剪断地问题要先确认"
        )
        self.assertFalse(out["agree"])
        self.assertEqual(len(out["spans"]), 1, out["spans"])
        self.assertEqual(out["spans"][0]["local"], "端")
        # 雲端那側已經繁化（s2twp）：要比的永遠是「字」，不是字形。
        self.assertEqual(out["spans"][0]["cloud"], "斷")

    def test_punctuation_only_difference_is_not_a_disagreement(self):
        out = transcribe.compare_transcripts("先確認", "先確認！")
        self.assertTrue(out["agree"])

    def test_arabic_versus_chinese_numeral_is_formatting_not_error(self):
        # 實測同一句就會出現「10 點」對「十点」：那不是聽錯，不能算進不一致率。
        out = transcribe.compare_transcripts("我們明天早上10點開會", "我們明天早上十點開會")
        self.assertTrue(out["agree"], out)
        self.assertEqual(out["spans"], [])
        self.assertEqual(out["formatting"], [{"local": "10", "cloud": "十", "at": 6}])
        self.assertIn("數字格式", transcribe.describe_second_opinion({"status": "ok", **out}))

    def test_empty_cloud_is_disagreement(self):
        out = transcribe.compare_transcripts("有講話", "")
        self.assertFalse(out["agree"])
        self.assertEqual(out["cloud_chars"], 0)


class PunctuationTests(unittest.TestCase):
    def test_profile_counts_marks(self):
        profile = transcribe.punctuation_profile("先確認，再說。")
        self.assertEqual(profile["total"], 2)
        self.assertEqual(profile["counts"], {"，": 1, "。": 1})
        self.assertEqual(profile["marks"], "，。")

    def test_local_without_marks_vs_cloud_with_marks(self):
        out = transcribe.compare_punctuation("操作方法有一樣嗎", "操作方法有一樣嗎？")
        self.assertFalse(out["same"])
        self.assertEqual((out["local_total"], out["cloud_total"]), (0, 1))
        self.assertEqual(out["cloud_counts"], {"？": 1})

    def test_identical_punctuation(self):
        out = transcribe.compare_punctuation("先確認。", "先確認。")
        self.assertTrue(out["same"])

    def test_halfwidth_comma_is_not_a_punctuation_difference(self):
        # 本地（Whisper）常給半形逗號、雲端給全形；那是編碼差異，不是斷句差異。
        out = transcribe.compare_punctuation("先確認,再說。", "先確認，再說。")
        self.assertTrue(out["same"], out)

    def test_describe_mentions_punctuation_delta(self):
        line = transcribe.describe_second_opinion(
            {
                "status": "ok",
                "agree": True,
                "elapsed_ms": 1000,
                "spans": [],
                "punctuation": {"local_total": 0, "cloud_total": 2},
            }
        )
        self.assertIn("標點 0→2", line)


class RefinedTests(unittest.TestCase):
    def test_reads_refined_file_when_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "refine.json"
            path.write_text(
                json.dumps({"ok": True, "text": "操作方法有一樣嗎？"}, ensure_ascii=False),
                encoding="utf-8",
            )
            self.assertEqual(
                transcribe.read_refined_text(str(path), 0), "操作方法有一樣嗎？"
            )

    def test_missing_or_failed_refine_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "refine.json"
            path.write_text(json.dumps({"ok": False, "text": "x"}), encoding="utf-8")
            self.assertIsNone(transcribe.read_refined_text(str(path), 0))
            self.assertIsNone(transcribe.read_refined_text(None, 0))

    def test_record_carries_refined_punctuation_comparison(self):
        def fake(*_args, **_kwargs):
            return _response("操作方法有一樣嗎？")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "refine.json"
            path.write_text(
                json.dumps({"ok": True, "text": "操作方法有一樣嗎。"}, ensure_ascii=False),
                encoding="utf-8",
            )
            record = transcribe.second_opinion_record(
                b"RIFF",
                "操作方法有一樣嗎",
                SETTINGS,
                transport=fake,
                token="t",
                refine_path=str(path),
                refine_wait=0,
            )
        self.assertEqual(record["refined"], "操作方法有一樣嗎。")
        self.assertFalse(record["punctuation_refined_vs_cloud"]["same"])
        self.assertTrue(record["punctuation"]["same"] is False)


class RequestTests(unittest.TestCase):
    def test_request_targets_transcribe_model_with_inline_audio(self):
        url, headers, payload = transcribe.build_cloud_transcribe_request(
            b"RIFFxxxx", SETTINGS
        )
        self.assertIn("/models/gemini-3.5-transcribe:generateContent", url)
        self.assertIn("projects/project-test/locations/global", url)
        self.assertEqual(headers["Content-Type"], "application/json")
        inline = payload["contents"][0]["parts"][0]["inline_data"]
        self.assertEqual(inline["mime_type"], "audio/wav")
        self.assertEqual(base64.b64decode(inline["data"]), b"RIFFxxxx")

    def test_unknown_model_is_overridable(self):
        url, _, _ = transcribe.build_cloud_transcribe_request(
            b"x", SETTINGS, "gemini-3.5-transcribe-live"
        )
        self.assertIn("gemini-3.5-transcribe-live", url)


class ParseTests(unittest.TestCase):
    def test_reads_audio_transcription_field(self):
        text, tin, tout = transcribe.parse_cloud_transcript(_response("我们明天开会"))
        self.assertEqual(text, "我们明天开会")
        self.assertEqual((tin, tout), (210, 28))

    def test_falls_back_to_plain_text_part(self):
        text, _, _ = transcribe.parse_cloud_transcript(_response("hello", style="text"))
        self.assertEqual(text, "hello")

    def test_duplicate_chunks_are_not_doubled(self):
        payload = _response("同一句")
        payload["candidates"][0]["content"]["parts"].append({"text": "同一句"})
        text, _, _ = transcribe.parse_cloud_transcript(payload)
        self.assertEqual(text, "同一句")


class CloudCallTests(unittest.TestCase):
    def test_transport_receives_bearer_token_and_returns_text(self):
        seen = {}

        def fake(url, headers, payload, timeout):
            seen["url"], seen["auth"], seen["timeout"] = url, headers["Authorization"], timeout
            return _response("我们明天开会")

        out = transcribe.cloud_transcribe_audio(
            b"RIFF", SETTINGS, transport=fake, token="tok-123", timeout=7
        )
        self.assertEqual(seen["auth"], "Bearer tok-123")
        self.assertEqual(seen["timeout"], 7)
        self.assertEqual(out["text"], "我们明天开会")
        self.assertEqual(out["input_tokens"], 210)

    def test_transport_failure_becomes_error_record(self):
        def boom(*_args, **_kwargs):
            raise RuntimeError("Vertex 呼叫失敗：TimeoutError")

        record = transcribe.second_opinion_record(
            b"RIFF", "本地文字", SETTINGS, transport=boom, token="t"
        )
        self.assertEqual(record["status"], "error")
        self.assertIn("TimeoutError", record["error"])

    def test_ok_record_carries_comparison_and_traditional_cloud(self):
        def fake(*_args, **_kwargs):
            return _response("我们明天早上十点开会")

        record = transcribe.second_opinion_record(
            b"RIFF", "我們明天早上十點開會", SETTINGS, seconds=8.4, transport=fake, token="t"
        )
        self.assertEqual(record["status"], "ok")
        self.assertTrue(record["agree"], record)
        self.assertEqual(record["cloud"], "我們明天早上十點開會")
        self.assertEqual(record["seconds"], 8.4)
        self.assertEqual(record["model"], transcribe.SECOND_OPINION_MODEL)


class LogTests(unittest.TestCase):
    def test_append_and_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "second.jsonl"
            transcribe._append_jsonl(
                str(path),
                {
                    "ts": "2026-10-27T10:00:00+07:00",
                    "status": "ok",
                    "local": "裁剪端地",
                    "cloud": "裁剪断地",
                    "agree": False,
                    "spans": [{"local": "端", "cloud": "断", "at": 2}],
                    "elapsed_ms": 1800,
                    "input_tokens": 210,
                    "output_tokens": 28,
                },
            )
            transcribe._append_jsonl(
                str(path),
                {
                    "ts": "2026-10-27T10:01:00+07:00",
                    "status": "ok",
                    "local": "一樣",
                    "cloud": "一樣",
                    "agree": True,
                    "spans": [],
                    "elapsed_ms": 1200,
                    "input_tokens": 100,
                    "output_tokens": 10,
                },
            )
            report = transcribe.second_opinion_report(str(path))
        self.assertIn("樣本 2", report)
        self.assertIn("一致 1、不一致 1", report)
        self.assertIn("一致率 50%", report)
        self.assertIn("端 → 断  ×1", report)
        self.assertIn("中位 1500ms", report)  # (1200+1800)/2，不是取中間那一筆

    def test_report_without_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = transcribe.second_opinion_report(str(Path(tmp) / "missing.jsonl"))
        self.assertIn("還沒有任何記錄", report)


if __name__ == "__main__":
    unittest.main()
