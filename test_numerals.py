import unittest

import transcribe


class DecimalNumeralTests(unittest.TestCase):
    def test_basic_decimals(self):
        self.assertEqual(transcribe.convert_decimal_numerals("二點一"), "2.1")
        self.assertEqual(transcribe.convert_decimal_numerals("二點五"), "2.5")
        self.assertEqual(transcribe.convert_decimal_numerals("一點七B的ASR"), "1.7B的ASR")
        self.assertEqual(transcribe.convert_decimal_numerals("零點五"), "0.5")

    def test_multi_digit_decimals_and_liang(self):
        c = transcribe.convert_numerals
        self.assertEqual(c("三百五十六點七"), "356.7")
        self.assertEqual(c("一百二十點五"), "120.5")
        self.assertEqual(c("兩點"), "2點")
        self.assertEqual(c("兩萬"), "2萬")

    def test_plain_integers(self):
        c = transcribe.convert_numerals
        self.assertEqual(c("一千零五十六"), "1056")
        self.assertEqual(c("一千零五"), "1005")
        self.assertEqual(c("二十五個人"), "25個人")
        self.assertEqual(c("二十分鐘"), "20分鐘")

    def test_colloquial_and_digit_by_digit_not_converted(self):
        c = transcribe.convert_numerals
        self.assertEqual(c("一個人"), "一個人")
        self.assertEqual(c("十分好"), "十分好")
        self.assertEqual(c("百分之三"), "百分之三")
        self.assertEqual(c("一百分之三"), "一百分之三")
        self.assertEqual(c("一二三"), "一二三")

    def test_time_and_teens_are_not_converted(self):
        self.assertEqual(transcribe.convert_decimal_numerals("十點三十分"), "十點三十分")
        self.assertEqual(transcribe.convert_decimal_numerals("三點半"), "三點半")
        self.assertEqual(transcribe.convert_decimal_numerals("有一點點事"), "有一點點事")

    def test_wan_yi_units(self):
        c = transcribe.convert_numerals
        self.assertEqual(c("二十萬元"), "20萬元")
        self.assertEqual(c("三百五十六點七萬"), "356.7萬")
        self.assertEqual(c("一百二十萬"), "120萬")
        self.assertEqual(c("三億"), "3億")
        self.assertEqual(c("一億二千萬"), "一億二千萬")  # 複合寫法整段不轉
        self.assertEqual(c("萬一"), "萬一")

    def test_time_expressions(self):
        c = transcribe.convert_numerals
        self.assertEqual(c("早上十點二十五分"), "早上10點25分")
        self.assertEqual(c("中午十二點整"), "中午12點整")
        self.assertEqual(c("晚上七點"), "晚上7點")
        self.assertEqual(c("三點半"), "3點半")
        self.assertEqual(c("有一點點事"), "有一點點事")
        self.assertEqual(c("十點六十分"), "十點六十分")  # 分鐘超過 59 不轉

    def test_rules_path_converts(self):
        out = transcribe.rules_rewrite_text("版本是二點一")
        self.assertIn("2.1", out)

    def test_settle_path_converts(self):
        self.assertIn("2.5", transcribe._settle_text("版本是二點五"))


if __name__ == "__main__":
    unittest.main()
