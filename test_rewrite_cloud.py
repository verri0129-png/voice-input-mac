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

    def test_learned_edit_without_usable_cursor_anchor(self):
        # 真實情境：ContextCapture 只抓到 after=1 個字，而該字在欄位裡重複出現，
        # 錨點無法唯一定位。改由前後兩份欄位快照 diff 出插入區塊，仍要學到。
        inserted = "原有工廠下載c級部reject的部bkcc跟nvy其餘維持reject"
        original_field = "會議紀錄 布況確認 請留意 布"
        current_field = original_field + inserted
        current_field = current_field.replace("c級部", "c級布", 1)
        payload = {
            "original_field": original_field,
            "inserted_text": inserted,
            "text_before": "",
            "text_after": "布",  # 在 original_field 出現兩次 → 錨點定位失敗
            "current_field": current_field,
        }
        candidates = transcribe.infer_term_candidates(payload)
        self.assertEqual(
            [(candidate.text, candidate.variants) for candidate in candidates],
            [("c級布re", ("c級部re",))],
        )

    def test_learned_edit_in_empty_field(self):
        payload = {
            "original_field": "",
            "inserted_text": "sketcher的Barry提出29卷",
            "text_before": "",
            "text_after": "",
            "current_field": "sketcher的Barry提出29捲",
        }
        candidates = transcribe.infer_term_candidates(payload)
        self.assertEqual(
            [(candidate.text, candidate.variants) for candidate in candidates],
            [("29捲", ("29卷",))],
        )

    def test_validate_names_the_disturbed_identifier(self):
        ok, reason, _ = rewrite_cloud.validate_rewrite(
            "原有的bkcc跟nvy要維持reject",
            "原有的bkcc跟NVY要維持reject",
        )
        self.assertFalse(ok)
        self.assertIn("nvy → NVY", reason)

    def test_learned_edit_allows_english_word_swap(self):
        # A1：英文專有名詞的修正（含長度改變）要學得到，例如 Sony → Sonia。
        inserted = "Sony在10月26號早上也通知生產要把那29卷的C級布自己挑片出來用"
        payload = {
            "original_field": "備忘錄",
            "inserted_text": inserted,
            "text_before": "備忘錄",
            "text_after": "",
            "current_field": "備忘錄" + inserted.replace("Sony", "Sonia"),
        }
        candidates = transcribe.infer_term_candidates(payload)
        self.assertEqual(len(candidates), 1)
        self.assertIn("Sonia", candidates[0].text)
        self.assertIn("Sony", candidates[0].variants[0])

    def test_learned_edit_still_rejects_digit_changes(self):
        inserted = "29卷的C級布"
        payload = {
            "original_field": "備忘錄",
            "inserted_text": inserted,
            "text_before": "備忘錄",
            "text_after": "",
            "current_field": "備忘錄" + inserted.replace("29", "39"),
        }
        self.assertEqual(transcribe.infer_term_candidates(payload), [])

    def test_rewrite_repairs_disturbed_identifier(self):
        draft = "Sketcher的Barry要求工廠把29卷C級布挑出來用"
        candidate = "Sketcher的Barry要求工廠把29捲C級布挑出來用".replace("Sketcher", "Skechers")
        ok, reason, _, text = rewrite_cloud.validate_and_repair_rewrite(draft, candidate)
        self.assertTrue(ok, reason)
        self.assertIn("Sketcher", text)
        self.assertNotIn("Skechers", text)
        self.assertIn("代號已還原", reason)

    def test_rewrite_rejects_when_identifiers_cannot_be_aligned(self):
        draft = "Sketcher要求工廠挑布"
        candidate = "Sketcher與Barry要求工廠挑布"
        ok, reason, _, _ = rewrite_cloud.validate_and_repair_rewrite(draft, candidate)
        self.assertFalse(ok)
        self.assertIn("無法對齊", reason)

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
        path, context, refine = transcribe.parse_serve_request("/tmp/a.wav")
        self.assertEqual((path, context, refine), ("/tmp/a.wav", {}, None))
        path, context, refine = transcribe.parse_serve_request(
            json.dumps({"wav": "/tmp/b.wav", "context": {"app_name": "Slack"}})
        )
        self.assertEqual((path, context, refine), ("/tmp/b.wav", {"app_name": "Slack"}, None))
        # Typeless 式定版：多一個 refine 檔案路徑
        path, context, refine = transcribe.parse_serve_request(
            json.dumps({"wav": "/tmp/c.wav", "refine": "/tmp/refine.json"})
        )
        self.assertEqual((path, context, refine), ("/tmp/c.wav", {}, "/tmp/refine.json"))
        with self.assertRaises(ValueError):
            transcribe.parse_serve_request('{"wav": 1}')
        with self.assertRaises(ValueError):
            transcribe.parse_serve_request(json.dumps({"wav": "/tmp/d.wav", "refine": 3}))

    def test_rules_path_always_ends_with_terminal_punct(self):
        # Breeze 的 raw 沒有標點，規則版是雲端失敗時使用者看到的唯一版本
        self.assertEqual(
            transcribe.rules_rewrite_text("因為我剛剛連續錄了兩次都沒問題中文字幕辨識的也都正確"),
            "因為我剛剛連續錄了兩次都沒問題中文字幕辨識的也都正確。",
        )
        # 已經有結尾標點就不再加
        self.assertEqual(transcribe.rules_rewrite_text("這樣可以嗎？"), "這樣可以嗎？")
        # 太短的不補（嘆詞、單字）
        self.assertEqual(transcribe.rules_rewrite_text("好"), "好")

    def test_rules_path_keeps_clause_commas_and_adds_period(self):
        self.assertEqual(
            transcribe.rules_rewrite_text("然後我進去之後才發現這個問題沒有那麼簡單而且還需要再確認一次細節"),
            "然後我進去之後，才發現這個問題沒有那麼簡單，而且還需要再確認一次細節。",
        )

    def test_rules_path_uses_question_mark_for_questions(self):
        # 10/19 M2 純規則版就有的疑問尾規則，改成先貼再定版時掉了
        self.assertEqual(
            transcribe.rules_rewrite_text("但是我錄了兩次當中那個字幕好像會先跑出來這是正常的對不對"),
            "但是我錄了兩次當中那個字幕好像會先跑出來這是正常的對不對？",
        )
        self.assertEqual(transcribe.rules_rewrite_text("這個尺寸還有庫存嗎"), "這個尺寸還有庫存嗎？")
        # 直述句不可以被誤判成問句
        self.assertEqual(
            transcribe.rules_rewrite_text("我不知道他為什麼沒來開會"),
            "我不知道他為什麼沒來開會。",
        )
        self.assertEqual(
            transcribe.rules_rewrite_text("這批布料今天下午會到廠"), "這批布料今天下午會到廠。"
        )


    def test_rules_path_applies_term_library(self):
        # 詞庫原本只進雲端 prompt，雲端 timeout 就等於沒有；規則版是使用者最常看到的那份
        rules = [rewrite_cloud.TermRule(text="詞庫", variants=("瓷庫", "磁庫"))]
        self.assertEqual(
            transcribe.rules_rewrite_text("幫我把瓷庫的檔案整理好嗎", rules),
            "幫我把詞庫的檔案整理好嗎？",
        )
        # 沒給規則時行為與舊版完全相同（純標點）
        self.assertEqual(
            transcribe.rules_rewrite_text("幫我把瓷庫的檔案整理好嗎"),
            "幫我把瓷庫的檔案整理好嗎？",
        )

    def test_apply_terms_is_noop_without_rules(self):
        self.assertEqual(transcribe.apply_terms("把瓷庫跟磁庫都同步", ()), "把瓷庫跟磁庫都同步")
        self.assertEqual(transcribe.apply_terms("把瓷庫跟磁庫都同步", None), "把瓷庫跟磁庫都同步")

    def test_apply_terms_prefers_longest_variant(self):
        # 短的先換會把「磁庫房」變成「詞庫房」，長變體就再也對不上
        rules = [
            rewrite_cloud.TermRule(text="詞庫", variants=("磁庫",)),
            rewrite_cloud.TermRule(text="倉庫", variants=("磁庫房",)),
        ]
        self.assertEqual(transcribe.apply_terms("磁庫房有兩間", rules), "倉庫有兩間")

    def test_settle_text_applies_term_library(self):
        rules = [rewrite_cloud.TermRule(text="詞庫", variants=("瓷庫",))]
        self.assertIn("詞庫", transcribe._settle_text("請更新瓷庫的說明文件", rules))
        self.assertIn("瓷庫", transcribe._settle_text("請更新瓷庫的說明文件"))


if __name__ == "__main__":
    unittest.main()
