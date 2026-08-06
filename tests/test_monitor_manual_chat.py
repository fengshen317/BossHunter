"""Tests for HR-initiated conversations handled as shadow jobs (manual_ prefix)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bosshunter.db import get_db, insert_job, update_job_status


def _job(job_id: str) -> dict:
    return {
        "id": job_id,
        "title": "AI运营",
        "company": "Example",
        "salary": "20-30K",
        "city": "北京",
        "experience": "1-3年",
        "jd": "负责AI内容运营",
        "hr_name": "HR",
        "hr_title": "招聘者",
        "hr_active": "",
        "company_size": "",
        "company_industry": "",
        "url": "https://example.com/job",
    }


class ManualJobIdTests(unittest.TestCase):
    def test_prefers_conversation_id(self):
        from bosshunter.executor.monitor import _manual_job_id

        self.assertEqual(_manual_job_id("王经理", "某科技", "conv-123"), "manual_conv-123")

    def test_falls_back_to_hr_company_hash_and_is_stable(self):
        from bosshunter.executor.monitor import _manual_job_id

        a = _manual_job_id("王经理", "某科技", "")
        b = _manual_job_id("王经理", "某科技", "")
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("manual_"))


class EnsureManualJobTests(unittest.TestCase):
    def _db(self, tmp: str):
        return get_db(Path(tmp) / "data" / "bosshunter.db")

    def test_creates_shadow_job_with_manual_status(self):
        from bosshunter.executor.monitor import _ensure_manual_job

        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            try:
                conv = {"hr_name": "王经理", "company": "某科技", "conversation_id": "conv-1"}
                job = _ensure_manual_job(db, conv)
                self.assertIsNotNone(job)
                self.assertEqual(job["id"], "manual_conv-1")
                self.assertEqual(job["status"], "manual")
                self.assertEqual(job["title"], "HR主动沟通")
                self.assertEqual(job["conversation_id"], "conv-1")
            finally:
                db.close()

    def test_is_idempotent(self):
        from bosshunter.executor.monitor import _ensure_manual_job

        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            try:
                conv = {"hr_name": "王经理", "company": "某科技", "conversation_id": "conv-1"}
                first = _ensure_manual_job(db, conv)
                second = _ensure_manual_job(db, conv)
                self.assertEqual(first["id"], second["id"])
                count = db.execute(
                    "SELECT COUNT(*) AS cnt FROM jobs WHERE id = ?", ("manual_conv-1",)
                ).fetchone()["cnt"]
                self.assertEqual(count, 1)
            finally:
                db.close()

    def test_missing_identity_returns_none(self):
        from bosshunter.executor.monitor import _ensure_manual_job

        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            try:
                self.assertIsNone(_ensure_manual_job(db, {}))
                self.assertIsNone(_ensure_manual_job(db, {"hr_name": "  "}))
            finally:
                db.close()


class CheckRepliesBuildsManualJobTests(unittest.TestCase):
    def test_unmatched_conversation_becomes_shadow_job(self):
        from bosshunter.executor import monitor

        # 一条已投递的岗位（跟踪列表非空），一条 HR 主动打招呼的未跟踪会话。
        conv_with_reply = {
            "hr_name": "李主管",
            "company": "新公司",
            "hr_title": "HR",
            "last_message": "您好，看了您的简历很感兴趣，方便聊聊吗？",
            "has_reply": False,
            "needs_inspection": True,
            "unread_count": 0,
            "is_our_message": False,
            "conversation_id": "conv-xyz",
            "element_index": 0,
        }

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data" / "bosshunter.db"
            db = get_db(db_path)
            db.close()

            def open_db():
                return get_db(db_path)

            with patch.object(monitor, "get_db", side_effect=open_db), \
                 patch.object(monitor, "new_tab", return_value="target-1"), \
                 patch.object(monitor, "wait_for_load", return_value=True), \
                 patch.object(monitor, "evaluate", return_value=json.dumps([conv_with_reply], ensure_ascii=False)), \
                 patch.object(monitor, "close_tab"), \
                 patch.object(monitor, "time") as mock_time:
                mock_time.sleep.return_value = None
                results = monitor.check_replies({"monitor": {}})

            verify_db = get_db(db_path)
            try:
                row = verify_db.execute(
                    "SELECT * FROM jobs WHERE id = ?", ("manual_conv-xyz",)
                ).fetchone()
            finally:
                verify_db.close()

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["job"]["id"], "manual_conv-xyz")
            self.assertEqual(results[0]["job"]["title"], "HR主动沟通")
            self.assertEqual(row["status"], "manual")


class PendingSameQuestionDedupTests(unittest.TestCase):
    def test_same_question_does_not_create_second_reply_pending(self):
        from bosshunter.executor import monitor

        messages = [
            {"sender": "me", "text": "您好，我对岗位很感兴趣。"},
            {"sender": "hr", "text": "方便介绍一下你的经验吗？"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data" / "bosshunter.db"
            db = get_db(db_path)
            try:
                insert_job(db, _job("job-1"))
                update_job_status(db, "job-1", "sent")
            finally:
                db.close()

            def open_db():
                return get_db(db_path)

            # 第一次：生成 reply_pending
            with patch.object(monitor, "get_db", side_effect=open_db), \
                 patch.object(monitor, "_open_conversation", return_value="target-1"), \
                 patch.object(monitor, "evaluate", return_value=json.dumps(messages, ensure_ascii=False)), \
                 patch.object(monitor, "close_tab"), \
                 patch.object(monitor, "_generate_auto_reply", return_value="可以，我介绍一下。"):
                action_first = monitor._handle_conversation(_job("job-1") | {"status": "sent"}, {"monitor": {}})

            # 第二次：同一问题应跳过，不再重复生成
            with patch.object(monitor, "get_db", side_effect=open_db), \
                 patch.object(monitor, "_open_conversation", return_value="target-1"), \
                 patch.object(monitor, "evaluate", return_value=json.dumps(messages, ensure_ascii=False)), \
                 patch.object(monitor, "close_tab"), \
                 patch.object(monitor, "_generate_auto_reply", return_value="可以，我介绍一下。"):
                action_second = monitor._handle_conversation(_job("job-1") | {"status": "sent"}, {"monitor": {}})

            verify_db = get_db(db_path)
            try:
                rows = verify_db.execute(
                    "SELECT action FROM history WHERE job_id = ? AND action = 'reply_pending' ORDER BY id",
                    ("job-1",),
                ).fetchall()
            finally:
                verify_db.close()

            self.assertEqual(action_first, "reply_pending")
            self.assertEqual(action_second, "skipped_dismissed_reply")
            self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
