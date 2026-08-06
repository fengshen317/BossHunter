"""Tests for targeted single-job import.

Covers:
- ``_generate_job_id``: detail-page + share-link URL variants resolve to the
  same id; unknown shapes fall back to a stable md5-derived id.
- ``_normalize_detail_url``: bare paths / protocol-relative / share links all
  normalize to a full desktop detail URL.
- ``import_job_by_url``: parses detail page, returns a complete job record,
  returns None when the page can't be opened or lacks title/company.
- ``search_jobs_by_company_title``: client-side company filter, idempotent url
  absolutization, respects ``max_results``.
"""

import json
import unittest
from unittest import mock
from unittest.mock import patch

from bosshunter.scraper.jobs import (
	_generate_job_id,
	_normalize_detail_url,
	import_job_by_url,
	search_jobs_by_company_title,
)


class GenerateJobIdTests(unittest.TestCase):
	def test_detail_page_html(self):
		self.assertEqual(
			_generate_job_id("https://www.zhipin.com/job_detail/abc123.html"),
			"abc123",
		)

	def test_detail_page_extensionless(self):
		self.assertEqual(
			_generate_job_id("/job_detail/abc123"),
			"abc123",
		)

	def test_share_job_link(self):
		# /share/job/{id} 短链也应解析出 id，否则手机端分享的链接无法导入。
		self.assertEqual(
			_generate_job_id("https://www.zhipin.com/share/job/xyz789?source=app"),
			"xyz789",
		)

	def test_detail_path_with_query_fragment(self):
		self.assertEqual(
			_generate_job_id("/job_detail/seg1.html?ref=x#anchor"),
			"seg1",
		)

	def test_unknown_shape_falls_back_to_stable_md5(self):
		# 形状不认识时退化为 md5；同一 URL 必须稳定，不同 URL 尽量不同。
		fallback = _generate_job_id("https://example.com/some/other/path")
		self.assertEqual(len(fallback), 16)
		self.assertEqual(fallback, _generate_job_id("https://example.com/some/other/path"))

	def test_empty_url_does_not_crash(self):
		# md5("") 仍是合法输出；调用方负责判空。
		self.assertEqual(len(_generate_job_id("")), 16)


class NormalizeDetailUrlTests(unittest.TestCase):
	def test_bare_path_gets_host(self):
		self.assertEqual(
			_normalize_detail_url("/job_detail/abc.html"),
			"https://www.zhipin.com/job_detail/abc.html",
		)

	def test_protocol_relative(self):
		self.assertEqual(
			_normalize_detail_url("//www.zhipin.com/job_detail/abc.html"),
			"https://www.zhipin.com/job_detail/abc.html",
		)

	def test_absolute_url_unchanged(self):
		url = "https://www.zhipin.com/job_detail/abc.html"
		self.assertEqual(_normalize_detail_url(url), url)

	def test_missing_protocol(self):
		self.assertEqual(
			_normalize_detail_url("www.zhipin.com/job_detail/abc.html"),
			"https://www.zhipin.com/job_detail/abc.html",
		)

	def test_share_link_promoted_to_detail(self):
		# 分享短链要转成标准详情页，让 JS_EXTRACT_DETAIL 统一解析。
		self.assertEqual(
			_normalize_detail_url("https://www.zhipin.com/share/job/xyz"),
			"https://www.zhipin.com/job_detail/xyz.html",
		)

	def test_empty_returns_empty(self):
		self.assertEqual(_normalize_detail_url(""), "")
		self.assertEqual(_normalize_detail_url("   "), "")


def _detail_payload(**overrides) -> str:
	"""模拟 JS_EXTRACT_DETAIL 的返回，给 import_job_by_url 用。"""
	detail = {
		"title": "Python 后端",
		"salary": "25-40K·14薪",
		"experience": "3-5年",
		"location": "北京·朝阳区·望京",
		"jd": "负责后端服务开发…",
		"company": "示例科技",
		"company_size": "100-499人",
		"company_industry": "人工智能",
		"company_background": "AI infra",
		"registered_capital": "1000万人民币",
		"hr_name": "王女士",
		"hr_title": "HRBP",
		"hr_active": "刚刚活跃",
	}
	detail.update(overrides)
	return json.dumps(detail)


class ImportJobByUrlTests(unittest.TestCase):
	def _patches(self, evaluate_return):
		"""mock 浏览器层 + 休眠，import_job_by_url 不应真的开标签页。"""
		return [
			patch("bosshunter.scraper.jobs.new_tab", return_value="tab-1"),
			patch("bosshunter.scraper.jobs.evaluate", return_value=evaluate_return),
			patch("bosshunter.scraper.jobs.close_tab"),
			patch("bosshunter.scraper.jobs.wait_for_load"),
			patch("bosshunter.scraper.jobs.scroll"),
			patch("bosshunter.scraper.jobs._wait_or_stop", return_value=False),
		]

	def test_parses_detail_into_full_record(self):
		with patch.multiple(
			"bosshunter.scraper.jobs",
			new_tab=mock.DEFAULT,
			evaluate=mock.DEFAULT,
			close_tab=mock.DEFAULT,
			wait_for_load=mock.DEFAULT,
			scroll=mock.DEFAULT,
			_wait_or_stop=mock.DEFAULT,
		) as mocks:
			mocks["new_tab"].return_value = "tab-1"
			mocks["evaluate"].return_value = _detail_payload()
			mocks["_wait_or_stop"].return_value = False
			job = import_job_by_url(
				"https://www.zhipin.com/job_detail/abc123.html", {}
			)
		self.assertIsNotNone(job)
		self.assertEqual(job["id"], "abc123")
		self.assertEqual(job["title"], "Python 后端")
		self.assertEqual(job["company"], "示例科技")
		self.assertEqual(job["hr_name"], "王女士")
		self.assertEqual(job["city"], "北京")
		self.assertEqual(job["district"], "朝阳区")
		# url 字段是规范化的完整 URL。
		self.assertTrue(job["url"].startswith("https://"))

	def test_normalizes_bare_path(self):
		# 只传 /job_detail/xxx.html 也能正确补全。
		with patch.multiple(
			"bosshunter.scraper.jobs",
			new_tab=mock.DEFAULT,
			evaluate=mock.DEFAULT,
			close_tab=mock.DEFAULT,
			wait_for_load=mock.DEFAULT,
			scroll=mock.DEFAULT,
			_wait_or_stop=mock.DEFAULT,
		) as mocks:
			mocks["new_tab"].return_value = "tab-1"
			mocks["evaluate"].return_value = _detail_payload()
			mocks["_wait_or_stop"].return_value = False
			job = import_job_by_url("/job_detail/seg2.html", {})
		self.assertEqual(job["id"], "seg2")

	def test_returns_none_when_new_tab_fails(self):
		# 详情页打不开（无 target_id）→ None，不抛异常。
		with patch("bosshunter.scraper.jobs.new_tab", return_value=""):
			self.assertIsNone(import_job_by_url("/job_detail/abc.html", {}))

	def test_returns_none_when_evaluate_empty(self):
		# 详情页解析为空 → None。
		for p in self._patches(None):
			p.start()
		try:
			self.assertIsNone(import_job_by_url("/job_detail/abc.html", {}))
		finally:
			for p in self._patches(None):
				p.stop()

	def test_returns_none_when_missing_title_or_company(self):
		# 详情页缺少职位名/公司名（疑似未登录或页面改版）→ None。
		for p in self._patches(_detail_payload(title="", company="")):
			p.start()
		try:
			self.assertIsNone(import_job_by_url("/job_detail/abc.html", {}))
		finally:
			for p in self._patches(_detail_payload(title="", company="")):
				p.stop()

	def test_returns_none_for_empty_url(self):
		self.assertIsNone(import_job_by_url("", {}))
		self.assertIsNone(import_job_by_url("   ", {}))

	def test_close_tab_always_called(self):
		# 即使 evaluate 失败，标签页也必须关闭，避免泄漏。
		with patch.multiple(
			"bosshunter.scraper.jobs",
			new_tab=mock.DEFAULT,
			evaluate=mock.DEFAULT,
			close_tab=mock.DEFAULT,
			wait_for_load=mock.DEFAULT,
			scroll=mock.DEFAULT,
			_wait_or_stop=mock.DEFAULT,
		) as mocks:
			mocks["new_tab"].return_value = "tab-1"
			mocks["evaluate"].return_value = None
			mocks["_wait_or_stop"].return_value = False
			import_job_by_url("/job_detail/abc.html", {})
			mocks["close_tab"].assert_called_once_with("tab-1")


class SearchJobsByCompanyTitleTests(unittest.TestCase):
	def _list_payload(self, cards) -> str:
		return json.dumps(cards)

	def _patches(self, evaluate_return):
		return [
			patch("bosshunter.scraper.jobs.new_tab", return_value="tab-1"),
			patch("bosshunter.scraper.jobs.evaluate", return_value=evaluate_return),
			patch("bosshunter.scraper.jobs.close_tab"),
			patch("bosshunter.scraper.jobs.wait_for_load"),
			patch("bosshunter.scraper.jobs.scroll"),
			patch("bosshunter.scraper.jobs._wait_or_stop", return_value=False),
		]

	def test_filters_by_company_client_side(self):
		# 搜索用 title 做 query，公司过滤要自己做：只保留公司名匹配的卡。
		cards = [
			{"title": "Java", "company": "示例科技", "salary": "20k", "url": "/job_detail/a.html"},
			{"title": "Java", "company": "别的公司", "salary": "15k", "url": "/job_detail/b.html"},
			{"title": "Java", "company": "示例科技有限公司", "salary": "30k", "url": "/job_detail/c.html"},
		]
		for p in self._patches(self._list_payload(cards)):
			p.start()
		try:
			results = search_jobs_by_company_title("示例科技", "Java", {})
		finally:
			for p in self._patches(self._list_payload(cards)):
				p.stop()
		# 模糊匹配：包含"示例科技"子串都算命中。
		companies = {r["company"] for r in results}
		self.assertEqual(companies, {"示例科技", "示例科技有限公司"})

	def test_absolute_url_in_candidates(self):
		cards = [
			{"title": "Java", "company": "X", "salary": "10k", "url": "/job_detail/abc.html"},
		]
		for p in self._patches(self._list_payload(cards)):
			p.start()
		try:
			results = search_jobs_by_company_title("X", "Java", {})
		finally:
			for p in self._patches(self._list_payload(cards)):
				p.stop()
		self.assertEqual(len(results), 1)
		self.assertTrue(results[0]["url"].startswith("https://www.zhipin.com/"))
		self.assertEqual(results[0]["id"], "abc")

	def test_max_results_cap(self):
		cards = [
			{"title": "Java", "company": "X", "salary": "", "url": f"/job_detail/n{i}.html"}
			for i in range(20)
		]
		for p in self._patches(self._list_payload(cards)):
			p.start()
		try:
			results = search_jobs_by_company_title("X", "Java", {}, max_results=3)
		finally:
			for p in self._patches(self._list_payload(cards)):
				p.stop()
		self.assertEqual(len(results), 3)

	def test_empty_when_no_title_and_no_company(self):
		# 两个都空时不应去打搜索页。
		with patch("bosshunter.scraper.jobs.new_tab") as mock_new_tab:
			results = search_jobs_by_company_title("", "", {})
		self.assertEqual(results, [])
		mock_new_tab.assert_not_called()

	def test_empty_when_search_returns_empty(self):
		for p in self._patches(self._list_payload([])):
			p.start()
		try:
			results = search_jobs_by_company_title("X", "Java", {})
		finally:
			for p in self._patches(self._list_payload([])):
				p.stop()
		self.assertEqual(results, [])

	def test_falls_back_to_company_as_keyword(self):
		# 只有公司名、没职位名时，用公司名当 query，避免空 query。
		# URL 会被 quote() 编码，所以断言用编码后的值。
		from urllib.parse import quote
		cards = [
			{"title": "Any", "company": "示例科技", "salary": "", "url": "/job_detail/a.html"},
		]
		for p in self._patches(self._list_payload(cards)):
			p.start()
		try:
			with patch("bosshunter.scraper.jobs.new_tab", wraps=lambda url: "tab-1") as spy:
				results = search_jobs_by_company_title("示例科技", "", {})
				opened_url = spy.call_args.args[0]
		finally:
			for p in self._patches(self._list_payload(cards)):
				p.stop()
		# query 里应当包含 URL-编码后的公司名。
		self.assertIn(quote("示例科技"), opened_url)
		self.assertEqual(len(results), 1)


if __name__ == "__main__":
	unittest.main()
