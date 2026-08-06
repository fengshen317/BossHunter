"""招呼语生成失败后的可见性与恢复回归测试。

Bug: 评分阶段先置 status='ready' 再批量生成招呼语；AI 返回空/异常时招呼语缺失，
岗位卡在 ready（sender 过滤空招呼语，永久不可见、不可发、无任何错误记录）。

修复:
1. scorer._recover_jobs_with_failed_greetings 把仍无招呼语的 ready 岗位回退到
   approved 并写 error 历史，让失败可见、可被 regenerate 恢复。
2. db.get_jobs_needing_greeting 同时包含 ready+空招呼语的岗位，使其能被重新生成。
"""

import tempfile
import unittest
from pathlib import Path

from bosshunter.db import get_db, insert_job, update_job_greeting, update_job_status


def _job(job_id: str) -> dict:
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


class RecoverFailedGreetingsTests(unittest.TestCase):
	"""_recover_jobs_with_failed_greetings 必须把卡住的 ready 岗位变可见、可恢复。"""

	def test_ready_job_with_empty_greeting_is_rolled_back_to_approved(self):
		"""ready 但招呼语为空 → 回退 approved + 写 error 历史。"""
		from bosshunter.ai.scorer import _recover_jobs_with_failed_greetings

		with tempfile.TemporaryDirectory() as tmp:
			db = get_db(Path(tmp) / "data" / "bosshunter.db")
			try:
				insert_job(db, _job("stuck-ready"))
				update_job_status(db, "stuck-ready", "ready")
				# 不设置 greeting（模拟 AI 生成失败）

				recovered = _recover_jobs_with_failed_greetings(db, [{"id": "stuck-ready"}])

				row = db.execute(
					"SELECT status, greeting FROM jobs WHERE id = ?", ("stuck-ready",)
				).fetchone()
				history = [
					r["action"]
					for r in db.execute(
						"SELECT action FROM history WHERE job_id = ?", ("stuck-ready",)
					).fetchall()
				]
			finally:
				db.close()

			self.assertEqual(recovered, 1)
			self.assertEqual(row["status"], "approved")
			self.assertEqual(history, ["error"])

	def test_ready_job_with_greeting_is_not_touched(self):
		"""已有招呼语的 ready 岗位不受影响。"""
		from bosshunter.ai.scorer import _recover_jobs_with_failed_greetings

		with tempfile.TemporaryDirectory() as tmp:
			db = get_db(Path(tmp) / "data" / "bosshunter.db")
			try:
				insert_job(db, _job("ok-ready"))
				update_job_status(db, "ok-ready", "ready")
				update_job_greeting(db, "ok-ready", "您好，我对岗位很感兴趣。")

				recovered = _recover_jobs_with_failed_greetings(db, [{"id": "ok-ready"}])

				row = db.execute(
					"SELECT status, greeting FROM jobs WHERE id = ?", ("ok-ready",)
				).fetchone()
				history = db.execute(
					"SELECT COUNT(*) AS c FROM history WHERE job_id = ?", ("ok-ready",)
				).fetchone()["c"]
			finally:
				db.close()

			self.assertEqual(recovered, 0)
			self.assertEqual(row["status"], "ready")
			self.assertIn("感兴趣", row["greeting"])
			self.assertEqual(history, 0)


class GetJobsNeedingGreetingTests(unittest.TestCase):
	"""get_jobs_needing_greeting 必须包含卡住的 ready 岗位，使其可被 regenerate 恢复。"""

	def test_includes_ready_jobs_with_empty_greeting(self):
		from bosshunter.db import get_jobs_needing_greeting

		with tempfile.TemporaryDirectory() as tmp:
			db = get_db(Path(tmp) / "data" / "bosshunter.db")
			try:
				# approved + 空招呼语（正常待生成）
				insert_job(db, _job("approved-empty"))
				update_job_status(db, "approved-empty", "approved")
				# ready + 空招呼语（卡住的失败岗位）
				insert_job(db, _job("ready-empty"))
				update_job_status(db, "ready-empty", "ready")
				# ready + 有招呼语（不应入选）
				insert_job(db, _job("ready-done"))
				update_job_status(db, "ready-done", "ready")
				update_job_greeting(db, "ready-done", "您好")

				jobs = get_jobs_needing_greeting(db)
				ids = {j["id"] for j in jobs}
			finally:
				db.close()

			self.assertIn("approved-empty", ids)
			self.assertIn("ready-empty", ids, "卡在 ready 的失败岗位必须能被重新生成")
			self.assertNotIn("ready-done", ids)


if __name__ == "__main__":
	unittest.main()
