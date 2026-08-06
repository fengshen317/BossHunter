"""Regression coverage for bounded concurrent deep scoring."""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bosshunter.ai.scorer import score_jobs
from bosshunter.db import get_db, get_jobs_by_status, insert_job


def _job(job_id: str) -> dict:
    """Build a minimal pending job fixture."""
    return {
        "id": job_id,
        "title": "AI 工程师",
        "company": "示例公司",
        "salary": "20-30K",
        "city": "深圳",
        "experience": "3年",
        "jd": "负责 AI 应用开发",
        "hr_name": "HR",
        "hr_title": "招聘者",
        "hr_active": "在线",
        "company_size": "100-499人",
        "company_industry": "人工智能",
        "url": f"https://example.com/{job_id}",
    }


class ParallelScoringTests(unittest.TestCase):
    """Ensure only provider calls, not SQLite access, are concurrent."""

    def test_deep_scoring_runs_concurrently_and_persists_results(self):
        """Three independent requests should overlap and be written safely."""
        active = 0
        max_active = 0
        lock = threading.Lock()
        release = threading.Event()

        def fake_call(_: str, __: dict) -> str:
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
                if active >= 2:
                    release.set()
            release.wait(1)
            time.sleep(0.01)
            with lock:
                active -= 1
            return '{"score": 88, "reason": "匹配", "missing": ""}'

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "jobs.db"
            resume_path = root / "resume.md"
            resume_path.write_text("AI 应用开发经验", encoding="utf-8")
            db = get_db(db_path)
            try:
                for index in range(3):
                    insert_job(db, _job(f"job-{index}"))
            finally:
                db.close()

            config = {
                "profile": {"resume_path": str(resume_path)},
                "scoring": {"threshold": 70, "concurrency": 3},
            }
            with patch("bosshunter.db.DB_PATH", db_path), \
                 patch("bosshunter.ai.scorer.quick_score", return_value=(100, "通过")), \
                 patch("bosshunter.ai.scorer._call_claude_with_retry", side_effect=fake_call), \
                 patch("bosshunter.ai.scorer.generate_greetings_batch", return_value=[]):
                scored, filtered = score_jobs(config)

            db = get_db(db_path)
            try:
                # 招呼语批量生成返回空（模拟失败）→ 卡住的 ready 岗位会被回退到
                # approved（避免 sender 过滤空招呼语导致永久不可见）。这里 mock 了
                # generate_greetings_batch 返回 []，所以 3 个岗位都应回退为 approved。
                approved_jobs = get_jobs_by_status(db, "approved")
                ready_jobs = get_jobs_by_status(db, "ready")
            finally:
                db.close()

        self.assertEqual((scored, filtered), (3, 0))
        self.assertEqual(len(ready_jobs), 0, "招呼语生成失败时不应停留在 ready")
        self.assertEqual(len(approved_jobs), 3, "招呼语失败应回退到 approved 以便重新生成")
        self.assertGreaterEqual(max_active, 2)
