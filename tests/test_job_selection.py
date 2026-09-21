import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from bosshunter.db import (
    get_db,
    get_jobs_approved_to_send,
    get_jobs_pending_confirmation,
    get_jobs_ready_to_send,
    get_jobs_with_send_errors,
    insert_job,
    reset_job_greeting,
    update_job_greeting,
    update_job_score,
    update_job_status,
)
from bosshunter.executor.sender import CHAT_BUTTON_SCRIPT_FOR_TESTS
from bosshunter.executor.sender import _chat_target_ids
from bosshunter.executor.sender import send_greetings
from bosshunter.executor.sender import _send_greeting_once
from bosshunter.executor.sender import _wait_for_chat_page


def _job(job_id: str, title: str = "Engineer") -> dict:
    return {
        "id": job_id,
        "title": title,
        "company": "Example",
        "salary": "10-20K",
        "city": "Beijing",
        "experience": "1-3 years",
        "jd": "Build product features",
        "hr_name": "HR",
        "hr_title": "Recruiter",
        "hr_active": "",
        "company_size": "",
        "company_industry": "",
        "url": "https://example.com/job",
    }


class JobSelectionTests(unittest.TestCase):
    def test_chat_button_script_prefers_real_anchor_over_visible_wrapper(self):
        script = CHAT_BUTTON_SCRIPT_FOR_TESTS

        self.assertIn("redirect-url", script)
        self.assertIn("data-url", script)

        redirect_pos = script.index("redirect-url")
        wrapper_pos = script.index("btn-startchat-wrap")
        self.assertLess(redirect_pos, wrapper_pos)

    def test_send_greeting_reports_unavailable_job_page_before_clicking_chat(self):
        job = {
            "id": "gone",
            "url": "https://www.zhipin.com/job_detail/gone.html",
        }

        with patch("bosshunter.executor.sender.new_tab", return_value="target-1"), \
             patch("bosshunter.executor.sender.evaluate", return_value='{"success": false, "error": "job_page_unavailable", "history_detail": "岗位页面不存在或已下架", "skip_backoff": true}'), \
             patch("bosshunter.executor.sender.close_tab") as close_tab, \
             patch("bosshunter.executor.sender.time.sleep"):
            result, target_id = _send_greeting_once(
                job,
                "您好，我对这个岗位很感兴趣。",
                {"browse_before_greet": False},
            )

        self.assertIsNone(target_id)
        self.assertEqual(result["error"], "job_page_unavailable")
        self.assertEqual(result["history_detail"], "岗位页面不存在或已下架")
        close_tab.assert_called_once_with("target-1")

    def test_send_greeting_uses_real_click_fallback_when_chat_button_does_not_navigate(self):
        job = {
            "id": "continue-chat",
            "url": "https://www.zhipin.com/job_detail/continue-chat.html",
        }
        evaluate_results = [
            '{"success": true}',  # page check
            '{"success": true, "button_text": "继续沟通"}',  # DOM click
            '{"success": true, "action": "no_popup"}',  # confirm popup
            '{"success": false, "path": "/job_detail/continue-chat.html"}',  # DOM click did not navigate
            '{"success": true, "action": "no_popup"}',  # confirm after real click
            '{"success": true, "path": "/web/geek/chat"}',  # real click reached chat
            '{"success": true}',  # send message
        ]

        with patch("bosshunter.executor.sender.new_tab", return_value="target-1"), \
             patch("bosshunter.executor.sender.evaluate", side_effect=evaluate_results), \
             patch("bosshunter.executor.sender.get_page_targets", return_value=[]), \
             patch("bosshunter.executor.sender.click_at", return_value=True) as click_at, \
             patch("bosshunter.executor.sender.close_tab") as close_tab, \
             patch("bosshunter.executor.sender.time.sleep"):
            result, target_id = _send_greeting_once(
                job,
                "您好，我对这个岗位很感兴趣。",
                {"browse_before_greet": False, "_chat_navigation_attempts": 1},
            )

        self.assertIsNone(target_id)
        self.assertTrue(result["success"])
        click_at.assert_called_once()
        fallback_selector = click_at.call_args.args[1]
        self.assertLess(fallback_selector.index("a.btn-startchat"), fallback_selector.index("btn-startchat-wrap"))
        close_tab.assert_called_once_with("target-1")

    def test_continue_chat_clicks_job_page_button_before_sending(self):
        """已有会话也必须从新打开的岗位页点击沟通入口。"""
        job = {
            "id": "continue-chat",
            "url": "https://www.zhipin.com/job_detail/continue-chat.html",
        }
        redirect_url = "/web/geek/chat?id=hr-id&jobId=continue-chat"
        evaluate_results = [
            '{"success": true}',  # page check
            json.dumps(
                {
                    "success": True,
                    "action": "clicked",
                    "button_text": "继续沟通",
                    "redirectUrl": redirect_url,
                }
            ),
            '{"success": true, "action": "no_popup"}',
            '{"success": true, "path": "/web/geek/chat", "hasInput": true}',
            '{"success": true}',  # send message
        ]

        with patch("bosshunter.executor.sender.new_tab", return_value="target-1"), \
             patch("bosshunter.executor.sender.evaluate", side_effect=evaluate_results), \
             patch("bosshunter.executor.sender.get_page_targets", return_value=[]), \
             patch("bosshunter.executor.sender.close_tab") as close_tab, \
             patch("bosshunter.executor.sender.time.sleep"):
            result, target_id = _send_greeting_once(
                job,
                "您好，我对这个岗位很感兴趣。",
                {"browse_before_greet": False, "_chat_navigation_attempts": 1},
            )

        self.assertIsNone(target_id)
        self.assertTrue(result["success"])
        self.assertEqual(result["conversation_id"], "hr-id")
        self.assertIn("btn.click();", CHAT_BUTTON_SCRIPT_FOR_TESTS)
        close_tab.assert_called_once_with("target-1")

    def test_send_greeting_waits_for_chat_button_before_failing(self):
        job = {
            "id": "slow-chat-button",
            "url": "https://www.zhipin.com/job_detail/slow-chat-button.html",
        }
        evaluate_results = [
            '{"success": true}',  # page check
            '{"success": false, "error": "no_chat_button"}',
            '{"success": true, "button_text": "继续沟通"}',
            '{"success": true, "action": "no_popup"}',
            '{"success": true, "path": "/web/geek/chat"}',
            '{"success": true}',
        ]

        with patch("bosshunter.executor.sender.new_tab", return_value="target-1"), \
             patch("bosshunter.executor.sender.evaluate", side_effect=evaluate_results) as evaluate_mock, \
             patch("bosshunter.executor.sender.get_page_targets", return_value=[]), \
             patch("bosshunter.executor.sender.close_tab") as close_tab, \
             patch("bosshunter.executor.sender.time.sleep"):
            result, target_id = _send_greeting_once(
                job,
                "您好，我对这个岗位很感兴趣。",
                {
                    "browse_before_greet": False,
                    "_chat_button_attempts": 2,
                    "_chat_navigation_attempts": 1,
                },
            )

        self.assertIsNone(target_id)
        self.assertTrue(result["success"])
        self.assertEqual(evaluate_mock.call_count, len(evaluate_results))
        close_tab.assert_called_once_with("target-1")

    def test_send_greeting_uses_chat_tab_opened_by_button(self):
        job = {
            "id": "new-chat-tab",
            "url": "https://www.zhipin.com/job_detail/new-chat-tab.html",
        }
        evaluate_results = [
            '{"success": true}',  # page check
            '{"success": true, "button_text": "继续沟通"}',
            '{"success": true, "action": "no_popup"}',
            '{"success": false, "path": "/job_detail/new-chat-tab.html"}',
            '{"success": true, "path": "/web/geek/chat", "hasInput": true}',
            '{"success": true}',  # send message in the new tab
        ]
        target_results = [
            [{"targetId": "target-1", "url": job["url"]}],
            [
                {"targetId": "target-1", "url": job["url"]},
                {"targetId": "target-chat", "url": "https://www.zhipin.com/web/geek/chat?id=hr"},
            ],
        ]

        with patch("bosshunter.executor.sender.new_tab", return_value="target-1"), \
             patch("bosshunter.executor.sender.evaluate", side_effect=evaluate_results) as evaluate_mock, \
             patch("bosshunter.executor.sender.get_page_targets", side_effect=target_results), \
             patch("bosshunter.executor.sender.close_tab") as close_tab, \
             patch("bosshunter.executor.sender.time.sleep"):
            result, target_id = _send_greeting_once(
                job,
                "您好，我对这个岗位很感兴趣。",
                {"browse_before_greet": False, "_chat_navigation_attempts": 1},
            )

        self.assertIsNone(target_id)
        self.assertTrue(result["success"])
        self.assertEqual(evaluate_mock.call_args.args[0], "target-chat")
        self.assertEqual(close_tab.call_args_list[0].args[0], "target-1")
        self.assertEqual(close_tab.call_args_list[1].args[0], "target-chat")

    def test_chat_target_ids_uses_runtime_target_id_contract(self):
        """真实运行时使用 targetId，测试必须覆盖该接口契约。"""
        with patch(
            "bosshunter.executor.sender.get_page_targets",
            return_value=[
                {"targetId": "runtime-target"},
                {"id": "legacy-target"},
                {"url": "https://example.com"},
            ],
        ):
            target_ids = _chat_target_ids()

        self.assertEqual(target_ids, {"runtime-target", "legacy-target"})

    def test_chat_list_page_without_visible_input_is_not_ready(self):
        """聊天列表页不能因为 URL 命中就被误判为具体会话。"""
        with patch(
            "bosshunter.executor.sender.evaluate",
            return_value='{"success": false, "path": "/web/geek/chat", "hasInput": false}',
        ), patch("bosshunter.executor.sender.time.sleep"):
            result = _wait_for_chat_page("chat-list", None, attempts=1)

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "chat_navigation_timeout")

    def test_send_greetings_retries_transient_failure_inside_job(self):
        """临时性失败（no_chat_input）现在由岗位内重试消化，最终成功计入已发送。"""
        job = _job("retry-chat-input")
        job["greeting"] = "您好，我对这个岗位很感兴趣。"

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "bosshunter.db"
            db = get_db(db_path)
            try:
                insert_job(db, job)
                # 发送队列只接受已确认岗位；重试测试直接以 approved 种子，
                # 与"人工确认后发送"的新规则保持一致。
                update_job_status(db, job["id"], "approved")
                update_job_greeting(db, job["id"], job["greeting"])
            finally:
                db.close()

            # 第一次 no_chat_input（重试），第二次成功。重试逻辑在
            # _send_greeting_with_retry 内部，外层 send_greetings 只调用一次。
            attempts = [
                ({"success": False, "error": "no_chat_input"}, "target-1"),
                ({"success": True}, None),
            ]

            with patch("bosshunter.db.DB_PATH", db_path), \
                 patch("bosshunter.executor.sender.should_take_day_off", return_value=False), \
                 patch("bosshunter.executor.sender.SendWindowChecker.is_active", return_value=True), \
                 patch("bosshunter.executor.sender._send_greeting_once", side_effect=attempts) as send_once, \
                 patch("bosshunter.executor.sender.close_tab") as close_tab, \
                 patch("bosshunter.executor.sender.time.sleep"):
                sent = send_greetings({"throttle": {"daily_limit": 10}}, force=True)

            self.assertEqual(sent, 1)
            self.assertEqual(send_once.call_count, 2)
            # 重试前的失败 target 已被 _send_greeting_once 内部失败分支清理，
            # 外层不再额外 close_tab（成功时 failed_target_id 为 None）。
            self.assertFalse(close_tab.called)

    def test_closed_job_is_persisted_as_terminal_status(self):
        """发送前发现岗位关闭时应进入终态，避免后续发送或回复跟踪。"""
        job = _job("closed-before-send")
        job["greeting"] = "您好，我对该岗位很感兴趣。"

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "bosshunter.db"
            db = get_db(db_path)
            try:
                insert_job(db, job)
                update_job_status(db, job["id"], "approved")
                update_job_greeting(db, job["id"], job["greeting"])
            finally:
                db.close()

            result = {
                "success": False,
                "error": "job_closed",
                "history_detail": "发送前检测到岗位已关闭",
                "skip_backoff": True,
            }
            with patch("bosshunter.db.DB_PATH", db_path), \
                 patch("bosshunter.executor.sender._send_greeting_once", return_value=(result, None)):
                sent = send_greetings({"throttle": {"daily_limit": 10}}, force=True)

            verify_db = get_db(db_path)
            try:
                status = verify_db.execute(
                    "SELECT status FROM jobs WHERE id = ?", (job["id"],)
                ).fetchone()["status"]
                action = verify_db.execute(
                    "SELECT action FROM history WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job["id"],)
                ).fetchone()["action"]
            finally:
                verify_db.close()

        self.assertEqual(sent, 0)
        self.assertEqual(status, "closed")
        self.assertEqual(action, "closed")

    def test_pending_confirmation_excludes_jobs_with_greetings(self):
        """确认清单现在包含有招呼语的 ready 岗位——招呼语在评分阶段已生成。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = get_db(Path(tmp) / "bosshunter.db")
            try:
                insert_job(db, _job("scored"))
                update_job_score(db, "scored", 88, "good match")
                update_job_status(db, "scored", "ready")

                insert_job(db, _job("sendable"))
                update_job_score(db, "sendable", 92, "great match")
                update_job_status(db, "sendable", "ready")
                update_job_greeting(db, "sendable", "Hi, this role looks like a strong fit.")

                jobs = get_jobs_pending_confirmation(db)
            finally:
                db.close()

        # 新行为：有招呼语的 ready 岗位也出现在确认清单中（按评分降序）
        self.assertEqual([job["id"] for job in jobs], ["sendable", "scored"])

    def test_ready_to_send_requires_a_non_empty_greeting(self):
        """发送队列只接受已人工确认(approved)且招呼语非空的岗位。

        ready（已评分未确认）岗位即使有招呼语也不可发送——这是"发送前必须
        人工确认"的强制点；error 岗位仅在显式手动重试时纳入。
        """
        with tempfile.TemporaryDirectory() as tmp:
            db = get_db(Path(tmp) / "bosshunter.db")
            try:
                insert_job(db, _job("no-greeting"))
                update_job_status(db, "no-greeting", "ready")

                insert_job(db, _job("blank-greeting"))
                update_job_status(db, "blank-greeting", "approved")
                update_job_greeting(db, "blank-greeting", "   ")

                insert_job(db, _job("ready-with-greeting"))
                update_job_status(db, "ready-with-greeting", "ready")
                update_job_greeting(db, "ready-with-greeting", "Hi, not confirmed yet.")

                insert_job(db, _job("approved"))
                update_job_status(db, "approved", "approved")
                update_job_greeting(db, "approved", "Confirmed, ready to send.")

                jobs = get_jobs_ready_to_send(db)
            finally:
                db.close()

        # ready 岗位（无论有无招呼语）都不应出现在发送队列
        self.assertCountEqual([job["id"] for job in jobs], ["approved"])

    def test_approved_to_send_excludes_ready_and_overlaps_no_confirmation(self):
        """「待发招呼语」只含 approved 岗位，与「今日待确定」(ready) 不重叠。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = get_db(Path(tmp) / "bosshunter.db")
            try:
                # ready + 招呼语 → 属于「今日待确定」，不应在「待发招呼语」
                insert_job(db, _job("ready-job"))
                update_job_status(db, "ready-job", "ready")
                update_job_greeting(db, "ready-job", "Hi, ready to confirm.")

                # approved + 招呼语 → 属于「待发招呼语」
                insert_job(db, _job("approved-job"))
                update_job_status(db, "approved-job", "approved")
                update_job_greeting(db, "approved-job", "Hi, ready to send.")

                # approved 但无招呼语 → 不属于「待发招呼语」
                insert_job(db, _job("no-greeting-approved"))
                update_job_status(db, "no-greeting-approved", "approved")

                # error + 招呼语 → 不属于「待发招呼语」
                insert_job(db, _job("errored"))
                update_job_status(db, "errored", "error")
                update_job_greeting(db, "errored", "Hi, failed before.")

                approved_to_send = get_jobs_approved_to_send(db)
                pending_confirmation = get_jobs_pending_confirmation(db)
            finally:
                db.close()

        self.assertCountEqual(
            [job["id"] for job in approved_to_send], ["approved-job"]
        )
        self.assertCountEqual(
            [job["id"] for job in pending_confirmation], ["ready-job"]
        )
        # 关键：两个区块的岗位 ID 没有任何交集
        approved_ids = {job["id"] for job in approved_to_send}
        ready_ids = {job["id"] for job in pending_confirmation}
        self.assertEqual(approved_ids & ready_ids, set())

    def test_send_errors_return_only_jobs_with_generated_greetings(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = get_db(Path(tmp) / "bosshunter.db")
            try:
                insert_job(db, _job("send-failed"))
                update_job_status(db, "send-failed", "error")
                update_job_greeting(db, "send-failed", "Hi, this role looks like a strong fit.")

                insert_job(db, _job("generation-failed"))
                update_job_status(db, "generation-failed", "error")

                insert_job(db, _job("sendable"))
                update_job_status(db, "sendable", "ready")
                update_job_greeting(db, "sendable", "Ready to send.")

                jobs = get_jobs_with_send_errors(db)
            finally:
                db.close()

        self.assertEqual([job["id"] for job in jobs], ["send-failed"])

    def test_explicit_retry_can_include_send_error_with_greeting(self):
        """工作台重试可取到 error 状态的已生成招呼语岗位。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = get_db(Path(tmp) / "bosshunter.db")
            try:
                insert_job(db, _job("send-failed"))
                update_job_status(db, "send-failed", "error")
                update_job_greeting(db, "send-failed", "Retry this greeting.")

                normal_jobs = get_jobs_ready_to_send(db)
                retry_jobs = get_jobs_ready_to_send(db, include_send_errors=True)
            finally:
                db.close()

        self.assertEqual(normal_jobs, [])
        self.assertEqual([job["id"] for job in retry_jobs], ["send-failed"])

    def test_selected_send_error_is_sent_when_manually_retried(self):
        """显式选择的失败岗位应进入发送器，而不再被 ready 筛选过滤。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "bosshunter.db"
            db = get_db(db_path)
            try:
                insert_job(db, _job("send-failed"))
                update_job_status(db, "send-failed", "error")
                update_job_greeting(db, "send-failed", "Retry this greeting.")
            finally:
                db.close()

            config = {
                "throttle": {"daily_limit": 10, "interval_min": 0, "interval_max": 0},
                "_workbench_job_ids": ["send-failed"],
                "_workbench_include_send_errors": True,
            }
            with patch("bosshunter.db.DB_PATH", db_path), \
                 patch("bosshunter.executor.sender._send_greeting_once", return_value=({"success": True}, None)):
                sent = send_greetings(config, force=True)

        self.assertEqual(sent, 1)

    def test_sent_job_can_be_reset_for_another_manual_greeting(self):
        """已联系岗位可重新生成，但必须回到人工确认链路。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = get_db(Path(tmp) / "bosshunter.db")
            try:
                insert_job(db, _job("sent-again"))
                update_job_status(db, "sent-again", "sent")
                update_job_greeting(db, "sent-again", "Old greeting")

                reset_job_greeting(db, ["sent-again"])
                job = db.execute(
                    "SELECT status, greeting FROM jobs WHERE id = ?", ("sent-again",)
                ).fetchone()
            finally:
                db.close()

        self.assertEqual(job["status"], "approved")
        self.assertEqual(job["greeting"], "")

    def test_send_greetings_force_bypasses_send_window_restriction(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "bosshunter.db"
            db = get_db(db_path)
            try:
                insert_job(db, _job("sendable"))
                # 发送前必须人工确认（approved）；force 只绕过窗口/休息日，
                # 不绕过确认规则。
                update_job_status(db, "sendable", "approved")
                update_job_greeting(db, "sendable", "Ready to send.")
            finally:
                db.close()

            config = {
                "throttle": {
                    "send_windows": ["09:00-16:00"],
                    "daily_limit": 30,
                    "interval_min": 0,
                    "interval_max": 0,
                    "browse_before_greet": False,
                }
            }

            # Act
            with patch("bosshunter.db.DB_PATH", db_path), \
                 patch("bosshunter.throttle.datetime") as mock_datetime, \
                 patch("bosshunter.executor.sender._send_greeting_once", return_value=({"success": True}, None)):
                mock_datetime.now.return_value = datetime(2026, 6, 19, 20, 0)
                sent = send_greetings(config, force=True)

            verify_db = get_db(db_path)
            try:
                status = verify_db.execute("SELECT status FROM jobs WHERE id = 'sendable'").fetchone()["status"]
                outside_window_events = verify_db.execute(
                    "SELECT COUNT(*) AS c FROM risk_events WHERE event_type = 'outside_window'"
                ).fetchone()["c"]
            finally:
                verify_db.close()

        # Assert
        self.assertEqual(sent, 1)
        self.assertEqual(status, "sent")
        self.assertEqual(outside_window_events, 0)


    def test_send_greeting_skips_job_with_existing_conversation(self):
        """已建立会话（conversation_id）的岗位绝不重发，防止同一 HR 收到重复招呼。

        这是"发送成功但记账未完成即崩溃"窗口的幂等兜底：即使岗位状态仍是
        approved，只要已有会话记录就跳过发送。
        """
        job = {
            "id": "already-chatted",
            "url": "https://www.zhipin.com/job_detail/already-chatted.html",
            "conversation_id": "hr-session-1",
        }
        with patch("bosshunter.executor.sender.new_tab") as new_tab, \
             patch("bosshunter.executor.sender._send_greeting_once") as send_once:
            result, _ = _send_greeting_once(job, "您好", {"browse_before_greet": False})

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "already_sent")
        new_tab.assert_not_called()
        send_once.assert_not_called()

    def test_ready_to_send_excludes_unconfirmed_ready_jobs(self):
        """发送队列只接受 approved：ready（未确认）岗位即使有招呼语也不可发。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = get_db(Path(tmp) / "bosshunter.db")
            try:
                insert_job(db, _job("ready-no-confirm"))
                update_job_status(db, "ready-no-confirm", "ready")
                update_job_greeting(db, "ready-no-confirm", "Hi, not confirmed yet.")

                insert_job(db, _job("approved-confirmed"))
                update_job_status(db, "approved-confirmed", "approved")
                update_job_greeting(db, "approved-confirmed", "Hi, confirmed.")

                jobs = get_jobs_ready_to_send(db)
            finally:
                db.close()

        self.assertCountEqual([job["id"] for job in jobs], ["approved-confirmed"])

    def test_include_send_errors_keeps_manual_retry_for_error_jobs(self):
        """显式手动重试（include_send_errors）仍可选中 error 岗位，常规发送不可。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = get_db(Path(tmp) / "bosshunter.db")
            try:
                insert_job(db, _job("failed-once"))
                update_job_status(db, "failed-once", "error")
                update_job_greeting(db, "failed-once", "Retry this.")

                normal = get_jobs_ready_to_send(db)
                manual = get_jobs_ready_to_send(db, include_send_errors=True)
            finally:
                db.close()

        self.assertEqual([job["id"] for job in normal], [])
        self.assertEqual([job["id"] for job in manual], ["failed-once"])


if __name__ == "__main__":
    unittest.main()
