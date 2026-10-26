import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import rewrite_cloud
import transcribe


class RewriteCloudTests(unittest.TestCase):
    def test_term_rules_accept_mapping_and_skip_bad_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "terms.json"
            path.write_text(
                json.dumps(
                    {
                        "terms": [
                            {"text": "會議共識", "match": ["會議公式"]},
                            {"text": "NVY"},
                            {"text": ""},
                            {"wrong": "shape"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            rules = rewrite_cloud.load_term_rules(path)
        self.assertEqual(
            [(rule.text, rule.variants) for rule in rules],
            [("會議共識", ("會議公式",)), ("NVY", ())],
        )

    def test_terms_prefer_aliases_found_in_draft(self):
        rules = [
            rewrite_cloud.TermRule(text="會議共識", variants=("會議公式",)),
            rewrite_cloud.TermRule(text="無關名詞", variants=()),
        ]
        selected = rewrite_cloud.select_terms("會議公式在今天討論", None, rules, 5)
        self.assertEqual([rule.text for rule in selected], ["會議共識"])

    def test_validator_accepts_declared_replacement_and_protects_codes(self):
        rules = [rewrite_cloud.TermRule(text="會議共識", variants=("會議公式",))]
        ok, _, changed = rewrite_cloud.validate_rewrite(
            "會議公式 BKCC 跟 NVY",
            "會議共識 BKCC 跟 NVY",
            declared_terms=rules,
        )
        self.assertTrue(ok)
        self.assertGreater(changed, 0)
        ok, reason, _ = rewrite_cloud.validate_rewrite(
            "訂單 NVY-2026 共 32 件",
            "訂單 NYV-2026 共 32 件",
            declared_terms=rules,
        )
        self.assertFalse(ok)
        self.assertIn("英文代號或數字", reason)

    def test_validator_rejects_added_meaning(self):
        ok, reason, _ = rewrite_cloud.validate_rewrite(
            "今天天氣很好",
            "今天天氣非常好而且適合出遊",
        )
        self.assertFalse(ok)
        self.assertIn("雲端", reason)

    def test_sanitize_context_truncates_url_and_blocks_sensitive(self):
        cleaned = rewrite_cloud.sanitize_context(
            {
                "app_name": "Slack",
                "text_before": "x" * 500,
                "web_url": "https://example.com/a?token=secret#frag",
                "field_role": "AXTextField",
            }
        )
        self.assertEqual(len(cleaned["text_before"]), rewrite_cloud.DEFAULT_CONTEXT_CHARS)
        self.assertEqual(cleaned["web_url"], "https://example.com/a")
        self.assertEqual(
            rewrite_cloud.sanitize_context({"blocked": True, "app_name": "Secret"}),
            {"blocked": True},
        )

    def test_prompt_includes_context_and_terms(self):
        system, user = rewrite_cloud.build_prompt(
            "會議公式在今天討論",
            {"app_name": "Slack", "text_before": "這批布", "text_after": "要處理"},
            [rewrite_cloud.TermRule(text="會議共識", variants=("會議公式",))],
        )
        self.assertIn("Slack", system)
        self.assertIn("會議共識", system)
        self.assertIn("會議公式在今天討論", user)

    def test_rewrite_draft_uses_transport_and_reports_usage(self):
        def fake_transport(url, headers, payload, timeout):
            self.assertIn("gemini-3.8-flash", url)
            self.assertEqual(headers["Content-Type"], "application/json")
            self.assertEqual(payload["generationConfig"]["temperature"], 0)
            return {
                "candidates": [{"content": {"parts": [{"text": "會議共識。"}]}}],
                "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 3},
            }

        config = rewrite_cloud.RewriteConfig(
            project="test-project", timeout_seconds=5, max_output_tokens=64
        )
        result = rewrite_cloud.rewrite_draft(
            "會議公式",
            context={"app_name": "Slack"},
            terms=[rewrite_cloud.TermRule(text="會議共識", variants=("會議公式",))],
            config=config,
            allowed_deletions=frozenset(),
            transport=fake_transport,
        )
        self.assertEqual(result.text, "會議共識。")
        self.assertEqual(result.input_tokens, 12)
        self.assertEqual(result.output_tokens, 3)

    def test_apply_rewrite_falls_back_to_rules_without_touching_words(self):
        original = transcribe.rewrite_draft
        transcribe.rewrite_draft = lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("no network")
        )
        try:
            final, info = transcribe.apply_rewrite(
                "今天天氣很好",
                context={"app_name": "Slack"},
                config=transcribe.rewrite_settings(backend="cloud"),
            )
        finally:
            transcribe.rewrite_draft = original
        self.assertEqual(info["backend"], "rules-fallback")
        self.assertEqual(final, "今天天氣很好")

    def test_learned_edit_creates_context_bound_correction(self):
        payload = {
            "original_field": "這是測試，稍後繼續。",
            "inserted_text": "會議公式",
            "text_before": "這是測試，",
            "text_after": "稍後繼續。",
            "current_field": "這是測試，會議共識稍後繼續。",
        }
        candidates = transcribe.infer_term_candidates(payload)
        self.assertEqual(
            [(candidate.text, candidate.variants) for candidate in candidates],
            [("會議共識", ("會議公式",))],
        )

    def test_learned_edit_merges_into_personal_terms(self):
        with tempfile.TemporaryDirectory() as tmp:
            terms_path = Path(tmp) / "terms.json"
            terms_path.write_text(json.dumps({"version": 1, "terms": []}), encoding="utf-8")
            payload = {
                "original_field": "這是測試，稍後繼續。",
                "inserted_text": "會議公式",
                "text_before": "這是測試，",
                "text_after": "稍後繼續。",
                "current_field": "這是測試，會議共識稍後繼續。",
            }
            summary = transcribe.learn_insertion_edits(payload, str(terms_path))
            saved = json.loads(terms_path.read_text(encoding="utf-8"))
        self.assertEqual(summary["learned"], 1)
        self.assertEqual(
            saved["terms"],
            [{"text": "會議共識", "match": ["會議公式"]}],
        )

    def test_learn_edit_cli_writes_terms(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload_path = Path(tmp) / "edit.json"
            payload_path.write_text(
                json.dumps(
                    {
                        "original_field": "這是測試，稍後繼續。",
                        "inserted_text": "會議公式",
                        "text_before": "這是測試，",
                        "text_after": "稍後繼續。",
                        "current_field": "這是測試，會議共識稍後繼續。",
                    }
                ),
                encoding="utf-8",
            )
            terms_path = Path(tmp) / "terms.json"
            terms_path.write_text(json.dumps({"version": 1, "terms": []}), encoding="utf-8")
            script = Path(__file__).with_name("transcribe.py")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--learn-edit-json",
                    str(payload_path),
                    "--terms-file",
                    str(terms_path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            saved = json.loads(terms_path.read_text(encoding="utf-8"))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("learned=1", completed.stdout)
        self.assertEqual(
            saved["terms"],
            [{"text": "會議共識", "match": ["會議公式"]}],
        )

    def test_parse_serve_request_supports_plain_and_context_forms(self):
        path, context = transcribe.parse_serve_request("/tmp/a.wav")
        self.assertEqual((path, context), ("/tmp/a.wav", {}))
        path, context = transcribe.parse_serve_request(
            json.dumps({"wav": "/tmp/b.wav", "context": {"app_name": "Slack"}})
        )
        self.assertEqual((path, context), ("/tmp/b.wav", {"app_name": "Slack"}))
        with self.assertRaises(ValueError):
            transcribe.parse_serve_request('{"wav": 1}')


if __name__ == "__main__":
    unittest.main()
