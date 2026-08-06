"""Tests for registered-capital parsing and hard filtering."""

import unittest

from bosshunter.ai.prefilter import quick_score
from bosshunter.config import load_config
from bosshunter.job_filters import is_agency_company, parse_registered_capital_wan


class ParseRegisteredCapitalWanTests(unittest.TestCase):
    def test_wan_unit(self):
        self.assertEqual(parse_registered_capital_wan("5000万"), 5000)

    def test_yi_unit_converts_to_wan(self):
        self.assertEqual(parse_registered_capital_wan("1亿"), 10000)

    def test_decimal_yi(self):
        self.assertEqual(parse_registered_capital_wan("1.5亿"), 15000)

    def test_with_trailing_unit_word(self):
        self.assertEqual(parse_registered_capital_wan("100万人民币"), 100)

    def test_exact_threshold(self):
        self.assertEqual(parse_registered_capital_wan("50万"), 50)

    def test_empty_and_none_return_none(self):
        self.assertIsNone(parse_registered_capital_wan(""))
        self.assertIsNone(parse_registered_capital_wan(None))

    def test_unparsable_returns_none(self):
        self.assertIsNone(parse_registered_capital_wan("未采集"))
        self.assertIsNone(parse_registered_capital_wan("abc"))


class QuickScoreRegisteredCapitalTests(unittest.TestCase):
    def _job(self, capital=None):
        return {
            "title": "AI工程师",
            "salary": "20-30K",
            "registered_capital": capital,
        }

    def _config(self, min_capital=50):
        return {
            "profile": {"deal_breakers": [], "allow_internship": False, "salary_min": 0},
            "scoring": {"min_registered_capital_wan": min_capital},
        }

    def test_missing_capital_is_kept(self):
        score, _ = quick_score(self._job(None), self._config())
        self.assertEqual(score, 100)

    def test_below_threshold_is_filtered(self):
        score, reason = quick_score(self._job("30万"), self._config())
        self.assertEqual(score, 0)
        self.assertIn("注册资本过低", reason)

    def test_equal_to_threshold_is_kept(self):
        score, _ = quick_score(self._job("50万"), self._config())
        self.assertEqual(score, 100)

    def test_high_capital_is_kept(self):
        score, _ = quick_score(self._job("5000万"), self._config())
        self.assertEqual(score, 100)

    def test_custom_threshold(self):
        # 阈值提高到 100 万后，50 万被过滤
        score, _ = quick_score(self._job("50万"), self._config(min_capital=100))
        self.assertEqual(score, 0)


class IsAgencyCompanyTests(unittest.TestCase):
    def test_vague_placeholder_company_is_agency(self):
        # 用户报告的典型代招模糊公司名
        self.assertTrue(is_agency_company("深圳某大型计算机软件公司"))
        self.assertTrue(is_agency_company("某知名互联网公司"))
        self.assertTrue(is_agency_company("某上市公司"))

    def test_keyword_agency_names(self):
        self.assertTrue(is_agency_company("XX人力资源有限公司"))
        self.assertTrue(is_agency_company("XX猎头顾问"))
        self.assertTrue(is_agency_company("XX劳务派遣公司"))
        self.assertTrue(is_agency_company("XX企业管理咨询有限公司"))

    def test_normal_company_is_kept(self):
        self.assertFalse(is_agency_company("腾讯科技"))
        self.assertFalse(is_agency_company("字节跳动有限公司"))
        self.assertFalse(is_agency_company("华为技术有限公司"))

    def test_empty_or_none_is_not_agency(self):
        self.assertFalse(is_agency_company(""))
        self.assertFalse(is_agency_company(None))

    def test_whitespace_only_is_not_agency(self):
        self.assertFalse(is_agency_company("   "))


class QuickScoreAgencyTests(unittest.TestCase):
    def _config(self):
        return {
            "profile": {"deal_breakers": [], "allow_internship": False, "salary_min": 0},
            "scoring": {"min_registered_capital_wan": 50},
        }

    def test_vague_company_is_prefiltered(self):
        job = {"title": "AI工程师", "salary": "20-30K", "company": "深圳某大型计算机软件公司"}
        score, reason = quick_score(job, self._config())
        self.assertEqual(score, 0)
        self.assertIn("代招", reason)

    def test_normal_company_passes_prefilter(self):
        job = {"title": "AI工程师", "salary": "20-30K", "company": "腾讯科技"}
        score, _ = quick_score(job, self._config())
        self.assertEqual(score, 100)

    def test_missing_company_passes_prefilter(self):
        job = {"title": "AI工程师", "salary": "20-30K", "company": ""}
        score, _ = quick_score(job, self._config())
        self.assertEqual(score, 100)


class ConfigDefaultTests(unittest.TestCase):
    def test_defaults_include_min_registered_capital(self):
        cfg = load_config()
        self.assertEqual(cfg["scoring"]["min_registered_capital_wan"], 50)


if __name__ == "__main__":
    unittest.main()
