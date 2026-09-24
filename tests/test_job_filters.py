"""Tests for registered-capital parsing and hard filtering."""

import unittest

from bosshunter.ai.prefilter import quick_score
from bosshunter.config import load_config
from bosshunter.job_filters import (
    is_agency_company,
    is_agency_job,
    is_whitelisted_company,
    matching_agency_phrase,
    parse_registered_capital_wan,
)


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


class MatchingAgencyPhraseTests(unittest.TestCase):
    """JD 外包特征短语匹配。

    下面的 JD 片段全部取自用户库内数据（BOSS 会在 JD 里插空格/换行，
    甚至把「【银行 外包 驻场】」断成三截）。
    """

    def test_real_outsourcing_jds_are_detected(self):
        cases = [
            # 汇合发展
            "外派驻场直聘招商银行（外包）：上班时间：8:30-12:00  14:00-17:30，双休",
            # 神州信息（注意 BOSS 注入的空格）
            "年度绩效考核奖金＋加班费，面试方式：现场一轮面试 3、【银行 外包 驻场】，长期稳定！！！",
            # 深圳网新新思
            "岗位介绍：平安智慧教育-知鸟AI（外包项目）当前项目周期到5月、后续有机会续签",
            # 信必优Symbio
            "工作地点：西安，需长期驻场，工作性质：全职",
            # 美云
            "Agent开发/模型微调/算法优化，岗位需求多人，人力外派",
            # 公众
            "本岗位为FDE前沿部署工程师角色，常驻联想现场，对接联想海外事业部",
        ]
        for jd in cases:
            with self.subTest(jd=jd[:24]):
                self.assertIsNotNone(matching_agency_phrase(jd), jd)

    def test_self_declared_non_outsourcing_is_not_detected(self):
        """反向自述：公司主动澄清"不是外包"，必须放过。

        这四条都是真实数据。盲目匹配"外包"二字会把这四家正常公司全部误杀。
        """
        cases = [
            # 华云中盛：整段 JD 里既有"非外包"声明，也有"FDE 驻场交付"，
            # 两个短语都不该命中（"驻场交付"会误伤 FDE 交付岗，故未收录）。
            "沉淀AI编程标准化使用方法论、排障SOP、交付模板，可独立带队指导新人快速上手FDE驻场交付工作。"
            "朋友们，不要再问我是不是外包岗位了T-T公司内部产品研发岗位，非外包非外包！！",
            # 鑫承视界
            "该岗位最终入职公司为子公司“深圳深度对齐科技有限公司”，非外包岗位。",
            # 谷米智能
            "背景要求：有大厂背景（BAT、字节、华为等），接受大厂外包/OD/驻场经历，我们看重大厂带来的规范",
            # 维尔迪
            "串联硬件固件、屏幕GUI、手机APP外包团队，统一产品AI交互逻辑",
        ]
        for jd in cases:
            with self.subTest(jd=jd[:24]):
                self.assertIsNone(matching_agency_phrase(jd), jd)

    def test_borderline_jds_are_left_alone(self):
        """边界岗位宁可漏掉也不要误杀。

        思谋科技、奥尼电子都是自研公司（奥尼是上市公司），"驻场"指的是
        短期出差去客户现场，不是常驻派遣。这两条是实测出来的误伤，已固化为回归。
        """
        cases = [
            # 思谋科技
            "学习与韧性：适应短期工厂驻场或产线调研，愿意在嘈杂环境中理解工人/工程师的真实操作习惯",
            # 奥尼电子：关键是"出差"
            "4.较强业务理解能力，能直面客户沟通，把业务语言转技术方案，接受短期/中长期客户现场驻场出差。",
            # 天锐计算机：乙方交付，但"驻场"指出差
            "能在现场独立推动问题解决；接受出差与驻场，适应客户现场工作节奏。",
        ]
        for jd in cases:
            with self.subTest(jd=jd[:24]):
                self.assertIsNone(matching_agency_phrase(jd), jd)

    def test_empty_jd_is_not_detected(self):
        self.assertIsNone(matching_agency_phrase(""))
        self.assertIsNone(matching_agency_phrase(None))


class IsAgencyJobTests(unittest.TestCase):
    def _config(self, **profile):
        base = {"deal_breakers": [], "allow_internship": False, "salary_min": 0}
        base.update(profile)
        return {"profile": base}

    def test_blocked_company_prefix_matches_truncated_name(self):
        """BOSS 会把公司名截断成"万宝盛华企业管理..."，短前缀要能命中。"""
        config = self._config(blocked_companies=["万宝盛华"])
        job = {"company": "万宝盛华企业管理...", "jd": "负责 AI 大模型应用开发。"}

        self.assertIn("公司黑名单", is_agency_job(job, config) or "")

    def test_blocked_company_is_case_insensitive(self):
        config = self._config(blocked_companies=["fesco"])
        self.assertIsNotNone(is_agency_job({"company": "FESCO Adecco", "jd": ""}, config))

    def test_clean_jd_and_company_passes(self):
        config = self._config(blocked_companies=["中软国际"])
        job = {"company": "腾讯科技", "jd": "负责企业级智能体应用的设计、开发与落地。"}

        self.assertIsNone(is_agency_job(job, config))

    def test_jd_scan_can_be_disabled(self):
        # 公司名必须是干净的，否则会被"某"占位符规则拦下，测不到 JD 开关。
        job = {"company": "示例科技", "jd": "本项目为外包项目，长期驻场。"}

        self.assertIsNotNone(is_agency_job(job, self._config(filter_agency_jobs=True)))
        self.assertIsNone(is_agency_job(job, self._config(filter_agency_jobs=False)))

    def test_placeholder_company_alone_is_enough(self):
        job = {"company": "某大型互联网公司", "jd": ""}
        self.assertIsNotNone(is_agency_job(job, self._config()))

    def test_missing_config_uses_defaults(self):
        """未传 config 时应按默认开启 JD 扫描，且不因缺字段报错。"""
        self.assertIsNotNone(is_agency_job({"company": "X", "jd": "需长期驻场"}))
        self.assertIsNone(is_agency_job({"company": "腾讯科技", "jd": "负责 Agent 开发"}))


class QuickScoreAgencyJobTests(unittest.TestCase):
    """外包过滤在 quick_score 里的端到端行为。"""

    def _config(self, **profile):
        base = {"deal_breakers": [], "allow_internship": False, "salary_min": 0}
        base.update(profile)
        return {"profile": base, "scoring": {"min_registered_capital_wan": 50}}

    def test_jd_outsourcing_is_prefiltered(self):
        job = {
            "title": "AI工程师", "salary": "20-30K", "company": "汇合发展",
            "jd": "外派驻场直聘招商银行（外包）：上班时间：8:30-12:00",
        }
        score, reason = quick_score(job, self._config())
        self.assertEqual(score, 0)
        self.assertIn("JD 外包特征", reason)

    def test_blocked_company_is_prefiltered_even_with_clean_jd(self):
        """中软国际、外企德科这类 JD 写得像甲方的，只能靠黑名单兜住。"""
        job = {
            "title": "智能体开发工程师", "salary": "20-30K", "company": "中软国际",
            "jd": "负责企业级智能体应用的设计、开发与落地，对接业务系统、模型服务和数据平台。",
        }
        score, reason = quick_score(job, self._config(blocked_companies=["中软国际"]))
        self.assertEqual(score, 0)
        self.assertIn("公司黑名单", reason)

    def test_self_declared_non_outsourcing_passes(self):
        job = {
            "title": "AI工程师", "salary": "20-30K", "company": "华云中盛",
            "jd": "不要再问我是不是外包岗位了T-T公司内部产品研发岗位，非外包非外包！！",
        }
        score, _ = quick_score(job, self._config())
        self.assertEqual(score, 100)


class WhitelistCompanyTests(unittest.TestCase):
    """白名单：命中的公司跳过全部自动过滤。"""

    def _config(self, **profile):
        base = {"deal_breakers": [], "allow_internship": False, "salary_min": 0}
        base.update(profile)
        return {"profile": base, "scoring": {"min_registered_capital_wan": 50}}

    def _job(self, company, **overrides):
        job = {"title": "AI工程师", "salary": "20-30K", "company": company}
        job.update(overrides)
        return job

    def test_prefix_matches_truncated_company_name(self):
        """BOSS 会把公司名截断，用短前缀要能命中所有华为子名。"""
        config = self._config(whitelist_companies=["华为"])
        for company in ("华为技术有限公司", "华为云计算", "深圳华为", "华为"):
            with self.subTest(company=company):
                self.assertTrue(is_whitelisted_company(company, config))

    def test_unrelated_company_is_not_whitelisted(self):
        config = self._config(whitelist_companies=["华为"])

        self.assertFalse(is_whitelisted_company("中软国际", config))
        self.assertFalse(is_whitelisted_company("腾讯", config))

    def test_empty_or_missing_whitelist(self):
        self.assertFalse(is_whitelisted_company("华为", self._config()))
        self.assertFalse(is_whitelisted_company("华为", self._config(whitelist_companies=[])))
        self.assertFalse(is_whitelisted_company("", self._config(whitelist_companies=["华为"])))
        self.assertFalse(is_whitelisted_company(None, {"profile": {"whitelist_companies": ["华为"]}}))

    def test_whitelist_beats_blocklist(self):
        """两个名单都命中时白名单优先：用户显式说"要"比关键词推断更可信。"""
        config = self._config(whitelist_companies=["华为"], blocked_companies=["华为"])

        self.assertIsNone(is_agency_job(self._job("华为技术有限公司"), config))

    def test_whitelisted_job_skips_every_prefilter(self):
        """白名单岗位不受一票否决词、实习、薪资、注册资本、外包 JD 任何一项影响。"""
        config = self._config(
            whitelist_companies=["华为"],
            deal_breakers=["外包"],
            salary_min=50,
        )
        job = self._job(
            "华为技术有限公司",
            title="外包实习生",          # 一票否决词 + 实习
            salary="5-8K",              # 低于 salary_min
            registered_capital="10万",  # 低于 min_registered_capital_wan
            jd="本项目为外包项目，需长期驻场。",  # 外包 JD 特征
        )

        score, reason = quick_score(job, config)

        self.assertEqual(score, 100)
        self.assertIn("白名单", reason)

    def test_non_whitelisted_job_still_filtered_by_same_rules(self):
        """同一套规则对非白名单公司照常生效，白名单没有把过滤整体关掉。"""
        config = self._config(whitelist_companies=["华为"], deal_breakers=["外包"])
        job = self._job("其它公司", title="外包工程师")

        score, reason = quick_score(job, config)

        self.assertEqual(score, 0)
        self.assertIn("触发排除词", reason)


if __name__ == "__main__":
    unittest.main()
