import json
import tempfile
import unittest
from pathlib import Path

from rewrite_cloud import TermRule, load_term_rules
from transcribe import apply_terms


FABLE = [TermRule(text="Fable", variants=("飛博",), fuzzy=True)]


class FuzzyTermTests(unittest.TestCase):
    def test_similar_english_words_become_term(self):
        for heard in ("Faber", "Fabio", "Fabre", "Fabel"):
            self.assertEqual(apply_terms(f"你去問{heard}吧", FABLE), "你去問Fable吧", heard)

    def test_real_words_and_other_first_letters_untouched(self):
        for word in ("Fabric", "table", "cable", "Fabulous", "fib", "file", "Face", "FAB", "Fails", "Falls", "Fables"):
            self.assertEqual(apply_terms(f"this {word} is", FABLE), f"this {word} is", word)

    def test_case_only_difference_not_touched(self):
        self.assertEqual(apply_terms("ask fable now", FABLE), "ask fable now")
        self.assertEqual(apply_terms("ask FABLE now", FABLE), "ask FABLE now")

    def test_exact_variants_still_work(self):
        self.assertEqual(apply_terms("讓飛博去想", FABLE), "讓Fable去想")

    def test_off_by_default(self):
        rules = [TermRule(text="Fable")]
        self.assertEqual(apply_terms("問Faber", rules), "問Faber")

    def test_short_term_allows_one_edit_only(self):
        rules = [TermRule(text="repo", fuzzy=True)]
        self.assertEqual(apply_terms("a repp b", rules), "a repo b")
        self.assertEqual(apply_terms("a reps b", rules), "a repo b")
        self.assertEqual(apply_terms("a rapport b", rules), "a rapport b")

    def test_non_latin_term_ignored(self):
        rules = [TermRule(text="詞庫", fuzzy=True)]
        self.assertEqual(apply_terms("瓷庫 ci", rules), "瓷庫 ci")

    def test_load_reads_fuzzy_flag(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "terms.json"
            p.write_text(json.dumps({"terms": [
                {"text": "Fable", "fuzzy": True},
                {"text": "Qwen", "fuzzy": "yes"},
                {"text": "Soniox"},
            ]}), encoding="utf-8")
            got = {r.text: r.fuzzy for r in load_term_rules(p)}
        self.assertEqual(got, {"Fable": True, "Qwen": False, "Soniox": False})


if __name__ == "__main__":
    unittest.main()
