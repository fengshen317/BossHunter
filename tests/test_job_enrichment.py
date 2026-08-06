import tempfile
import unittest
from pathlib import Path

from bosshunter.ai.scorer import SCORING_PROMPT
from bosshunter.db import get_db, get_job, insert_job, update_job_enrichment, update_job_hr_active
from bosshunter.scraper.jobs import _extract_district


class JobEnrichmentTests(unittest.TestCase):
    def test_extract_district_from_boss_location(self):
        self.assertEqual(_extract_district("深圳·南山区·科技园", "深圳"), "南山区")

    def test_stores_company_background_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = get_db(Path(tmp) / "jobs.db")
            try:
                insert_job(db, {
                    "id": "job-1",
                    "title": "AI 工程师",
                    "company": "示例公司",
                    "salary": "20K",
                    "city": "深圳",
                    "district": "南山区",
                    "experience": "3 年",
                    "jd": "开发智能体产品",
                    "hr_name": "",
                    "hr_title": "",
                    "hr_active": "",
                    "company_size": "100-499人",
                    "company_industry": "人工智能",
                    "company_background": "人工智能 · B轮",
                    "registered_capital": "1000万人民币",
                    "url": "https://example.com/job",
                })
                row = db.execute(
                    "SELECT district, company_background, registered_capital FROM jobs WHERE id = 'job-1'"
                ).fetchone()
            finally:
                db.close()

        self.assertEqual(tuple(row), ("南山区", "人工智能 · B轮", "1000万人民币"))

    def test_scoring_prompt_includes_company_and_location_factors(self):
        self.assertIn("公司背景", SCORING_PROMPT)
        self.assertIn("注册资本", SCORING_PROMPT)
        self.assertIn("具体区县", SCORING_PROMPT)

    def test_enrichment_only_fills_missing_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = get_db(Path(tmp) / "jobs.db")
            try:
                insert_job(db, {
                    "id": "job-2", "title": "工程师", "company": "示例公司", "salary": "20K",
                    "city": "深圳", "experience": "3 年", "jd": "开发", "hr_name": "", "hr_title": "",
                    "hr_active": "", "company_size": "", "company_industry": "人工智能",
                    "url": "https://example.com/job",
                })
                update_job_enrichment(db, {
                    "id": "job-2", "district": "南山区", "company_size": "100-499人",
                    "company_industry": "互联网", "company_background": "人工智能 · B轮",
                    "registered_capital": "1000万人民币",
                })
                row = get_job(db, "job-2")
            finally:
                db.close()

        self.assertEqual(row["district"], "南山区")
        self.assertEqual(row["company_industry"], "人工智能")
        self.assertEqual(row["registered_capital"], "1000万人民币")

    def test_hr_activity_refresh_replaces_old_snapshot(self):
        """发送前读取到的新活跃状态应覆盖采集阶段的旧快照。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = get_db(Path(tmp) / "jobs.db")
            try:
                insert_job(db, {
                    "id": "job-active", "title": "工程师", "company": "示例公司", "salary": "20K",
                    "city": "深圳", "experience": "3年", "jd": "开发", "hr_name": "", "hr_title": "",
                    "hr_active": "本周活跃", "company_size": "", "company_industry": "互联网",
                    "url": "https://example.com/job",
                })
                update_job_hr_active(db, "job-active", "在线")
                row = get_job(db, "job-active")
            finally:
                db.close()

        self.assertEqual(row["hr_active"], "在线")
