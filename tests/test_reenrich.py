"""Tests for the reenrich command: re-fill missing company/location metadata.

Covers:
- get_jobs_needing_enrichment: 只返回缺字段的岗位，支持 status 过滤
- reenrich_jobs: 重开详情页补全空字段，不动 score/status/greeting/jd
- 失败容错：URL 失效/详情页打不开时跳过不崩溃
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bosshunter.db import (
	get_db,
	get_jobs_needing_enrichment,
	insert_job,
	update_job_score,
	update_job_status,
)
from bosshunter.scraper.jobs import reenrich_jobs


def _full_job(job_id: str) -> dict:
	"""一条字段齐全的岗位（不应被 reenrich 选中）。"""
	return {
		"id": job_id, "title": "T", "company": "C", "salary": "10k", "city": "深圳",
		"experience": "3", "jd": "x", "hr_name": "H", "hr_title": "R", "hr_active": "",
		"company_size": "", "company_industry": "", "url": "http://x",
	}


class GetJobsNeedingEnrichmentTests(unittest.TestCase):
	def _seed(self, db):
		# 缺 district
		insert_job(db, _full_job("no-district"))
		update_job_status(db, "no-district", "ready")
		db.execute("UPDATE jobs SET district='' WHERE id='no-district'")
		# 缺 company_background
		insert_job(db, _full_job("no-bg"))
		update_job_status(db, "no-bg", "pending")
		db.execute("UPDATE jobs SET company_background='' WHERE id='no-bg'")
		# 缺 registered_capital
		insert_job(db, _full_job("no-cap"))
		update_job_status(db, "no-cap", "sent")
		db.execute("UPDATE jobs SET registered_capital='' WHERE id='no-cap'")
		# 字段全（不应被选中）
		insert_job(db, _full_job("full"))
		update_job_status(db, "full", "ready")
		db.execute(
			"UPDATE jobs SET district='南山', company_background='AI', registered_capital='100万' WHERE id='full'"
		)
		db.commit()

	def test_returns_only_jobs_missing_fields(self):
		with tempfile.TemporaryDirectory() as tmp:
			db = get_db(Path(tmp) / "t.db")
			try:
				self._seed(db)
				jobs = get_jobs_needing_enrichment(db)
			finally:
				db.close()
		ids = {j["id"] for j in jobs}
		self.assertEqual(ids, {"no-district", "no-bg", "no-cap"})

	def test_status_filter_restricts_results(self):
		with tempfile.TemporaryDirectory() as tmp:
			db = get_db(Path(tmp) / "t.db")
			try:
				self._seed(db)
				jobs = get_jobs_needing_enrichment(db, statuses=["ready"])
			finally:
				db.close()
		ids = {j["id"] for j in jobs}
		# 只有 no-district 是 ready 且缺字段；full 是 ready 但字段全
		self.assertEqual(ids, {"no-district"})

	def test_empty_db_returns_empty(self):
		with tempfile.TemporaryDirectory() as tmp:
			db = get_db(Path(tmp) / "t.db")
			try:
				self.assertEqual(get_jobs_needing_enrichment(db), [])
			finally:
				db.close()


class ReenrichJobsTests(unittest.TestCase):
	def _detail_json(self, **overrides) -> str:
		"""模拟 JS_EXTRACT_DETAIL 的返回。"""
		detail = {
			"location": "深圳·南山区", "company_size": "100-499人",
			"company_industry": "人工智能", "company_background": "AI科技公司",
			"registered_capital": "1000万人民币",
		}
		detail.update(overrides)
		return json.dumps(detail)

	def test_fills_missing_fields_without_touching_score_status(self):
		"""补全空字段，但 score/status/greeting 保持不变。"""
		with tempfile.TemporaryDirectory() as tmp:
			db_path = Path(tmp) / "t.db"
			db = get_db(db_path)
			try:
				insert_job(db, _full_job("stale"))
				update_job_status(db, "stale", "ready")
				update_job_score(db, "stale", 85, "高分")
				db.execute(
					"UPDATE jobs SET district='', company_background='', registered_capital='' "
					"WHERE id='stale'"
				)
				db.commit()
			finally:
				db.close()

			with patch("bosshunter.scraper.jobs.new_tab", return_value="tab-1"), \
				patch("bosshunter.scraper.jobs.evaluate", return_value=self._detail_json()), \
				patch("bosshunter.scraper.jobs.close_tab"), \
				patch("bosshunter.scraper.jobs.wait_for_load"), \
				patch("bosshunter.scraper.jobs.time.sleep"), \
				patch("bosshunter.scraper.jobs.PageThrottle"), \
				patch("bosshunter.scraper.jobs.get_db", return_value=get_db(db_path)):
				count = reenrich_jobs({})

			db = get_db(db_path)
			try:
				row = db.execute(
					"SELECT district, company_background, registered_capital, status, score FROM jobs WHERE id='stale'"
				).fetchone()
			finally:
				db.close()

		self.assertEqual(count, 1)
		self.assertEqual(row["district"], "南山区")
		self.assertEqual(row["company_background"], "AI科技公司")
		self.assertEqual(row["registered_capital"], "1000万人民币")
		# 关键：评分和状态不被改动
		self.assertEqual(row["status"], "ready")
		self.assertEqual(row["score"], 85)

	def test_limit_caps_batch_size(self):
		"""--limit 限制单批处理数量。"""
		with tempfile.TemporaryDirectory() as tmp:
			db_path = Path(tmp) / "t.db"
			db = get_db(db_path)
			try:
				for i in range(5):
					insert_job(db, _full_job(f"stale-{i}"))
					update_job_status(db, f"stale-{i}", "ready")
					db.execute(f"UPDATE jobs SET district='' WHERE id='stale-{i}'")
				db.commit()
			finally:
				db.close()

			opened_tabs = []
			def fake_new_tab(url):
				opened_tabs.append(url)
				return f"tab-{len(opened_tabs)}"

			with patch("bosshunter.scraper.jobs.get_db", return_value=get_db(db_path)), \
				patch("bosshunter.scraper.jobs.new_tab", side_effect=fake_new_tab), \
				patch("bosshunter.scraper.jobs.evaluate", return_value=self._detail_json()), \
				patch("bosshunter.scraper.jobs.close_tab"), \
				patch("bosshunter.scraper.jobs.wait_for_load"), \
				patch("bosshunter.scraper.jobs.time.sleep"), \
				patch("bosshunter.scraper.jobs.PageThrottle"):
				count = reenrich_jobs({}, limit=2)

		self.assertEqual(count, 2)
		self.assertEqual(len(opened_tabs), 2)

	def test_failed_detail_page_skipped(self):
		"""详情页打不开（evaluate 返回空）时跳过，不崩溃。"""
		with tempfile.TemporaryDirectory() as tmp:
			db_path = Path(tmp) / "t.db"
			db = get_db(db_path)
			try:
				insert_job(db, _full_job("gone"))
				update_job_status(db, "gone", "ready")
				db.execute("UPDATE jobs SET district='' WHERE id='gone'")
				db.commit()
			finally:
				db.close()

			with patch("bosshunter.scraper.jobs.get_db", return_value=get_db(db_path)), \
				patch("bosshunter.scraper.jobs.new_tab", return_value="tab-1"), \
				patch("bosshunter.scraper.jobs.evaluate", return_value=None), \
				patch("bosshunter.scraper.jobs.close_tab"), \
				patch("bosshunter.scraper.jobs.wait_for_load"), \
				patch("bosshunter.scraper.jobs.time.sleep"), \
				patch("bosshunter.scraper.jobs.PageThrottle"):
				count = reenrich_jobs({})

		self.assertEqual(count, 0)

	def test_no_jobs_returns_zero(self):
		"""没有缺字段岗位时返回 0，不报错。"""
		with tempfile.TemporaryDirectory() as tmp:
			db_path = Path(tmp) / "t.db"
			db = get_db(db_path)
			try:
				# 只插入字段全的岗位
				insert_job(db, _full_job("full"))
				update_job_status(db, "full", "ready")
				db.execute(
					"UPDATE jobs SET district='南山', company_background='AI', registered_capital='100万' WHERE id='full'"
				)
				db.commit()
			finally:
				db.close()

			with patch("bosshunter.scraper.jobs.get_db", return_value=get_db(db_path)):
				count = reenrich_jobs({})

		self.assertEqual(count, 0)


if __name__ == "__main__":
	unittest.main()
