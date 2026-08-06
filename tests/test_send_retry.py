"""Regression tests for send-greeting retry/recovery improvements.

Covers:
- in-job retries for transient failures (runtime hiccups, slow pages)
- permanent failures short-circuit retries
- ``no_chat_button`` preserves candidate diagnostics in history detail
- ``recover_send_errors`` re-queues jobs after the backoff window
- runtime-unavailable errors do not pollute progressive backoff
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bosshunter.db import (
	get_db,
	get_jobs_ready_to_send,
	insert_job,
	recover_send_errors,
	update_job_greeting,
	update_job_status,
	update_job_status_with_retry,
)
from bosshunter.executor.sender import (
	_classify_error,
	_describe_no_chat_button,
	_send_greeting_once,
	_send_greeting_with_retry,
	_verify_message_sent,
)


def _job(job_id: str) -> dict:
	return {
		"id": job_id,
		"title": "Engineer",
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
		"url": "https://www.zhipin.com/job_detail/" + job_id + ".html",
	}


class ClassifyErrorTests(unittest.TestCase):
	def test_transient_errors_are_retryable(self):
		for code in ("no_response", "no_chat_button", "no_chat_input", "chat_navigation_timeout", "runtime_not_ready"):
			self.assertEqual(_classify_error(code), "retryable")

	def test_permanent_errors_short_circuit(self):
		for code in ("job_page_unavailable", "send_button_disabled", "captcha", "rate_limit", "blocked"):
			self.assertEqual(_classify_error(code), "permanent")

	def test_stopped_is_handled_separately(self):
		self.assertEqual(_classify_error("stopped"), "stopped")


class SendGreetingWithRetryTests(unittest.TestCase):
	def test_transient_failure_is_retried_then_succeeds(self):
		"""no_chat_input on attempt 1, success on attempt 2 — only 2 calls."""
		job = _job("retry-then-ok")
		attempts = [
			({"success": False, "error": "no_chat_input", "skip_backoff": True}, "target-1"),
			({"success": True}, None),
		]
		with patch("bosshunter.executor.sender._send_greeting_once", side_effect=attempts) as send_once, \
			patch("bosshunter.executor.sender.time.sleep"), \
			patch("bosshunter.executor.sender.close_tab"):
			result, target_id = _send_greeting_with_retry(
				job, "您好", {"send_max_retries": 2, "send_retry_backoff_min": 0, "send_retry_backoff_max": 0}
			)

		self.assertTrue(result["success"])
		self.assertIsNone(target_id)
		self.assertEqual(send_once.call_count, 2)

	def test_permanent_failure_is_not_retried(self):
		"""job_page_unavailable returns immediately — only 1 call."""
		job = _job("gone-job")
		attempts = [({"success": False, "error": "job_page_unavailable", "skip_backoff": True}, None)]
		with patch("bosshunter.executor.sender._send_greeting_once", side_effect=attempts) as send_once, \
			patch("bosshunter.executor.sender.time.sleep"), \
			patch("bosshunter.executor.sender.close_tab"):
			result, _ = _send_greeting_with_retry(
				job, "您好", {"send_max_retries": 3, "send_retry_backoff_min": 0, "send_retry_backoff_max": 0}
			)

		self.assertFalse(result["success"])
		self.assertEqual(result["error"], "job_page_unavailable")
		self.assertEqual(send_once.call_count, 1)

	def test_retries_exhausted_returns_last_error(self):
		"""All retries fail with a transient error → last error returned."""
		job = _job("always-fail")
		attempts = [
			({"success": False, "error": "no_response"}, None),
			({"success": False, "error": "no_response"}, None),
			({"success": False, "error": "no_response"}, None),
		]
		with patch("bosshunter.executor.sender._send_greeting_once", side_effect=attempts) as send_once, \
			patch("bosshunter.executor.sender.time.sleep"), \
			patch("bosshunter.executor.sender.close_tab"):
			result, _ = _send_greeting_with_retry(
				job, "您好", {"send_max_retries": 2, "send_retry_backoff_min": 0, "send_retry_backoff_max": 0}
			)

		self.assertFalse(result["success"])
		self.assertEqual(result["error"], "no_response")
		# 1 initial + 2 retries
		self.assertEqual(send_once.call_count, 3)

	def test_stop_event_aborts_retry_loop(self):
		"""A stop event during backoff breaks out and returns stopped."""
		from threading import Event

		job = _job("stop-midway")
		stop_event = Event()
		# First attempt fails; the backoff wait will set the event.
		attempts = [({"success": False, "error": "no_chat_input"}, "target-1")]

		def _set_event_and_fail(seconds, evt):
			evt.set()
			return True

		with patch("bosshunter.executor.sender._send_greeting_once", side_effect=attempts), \
			patch("bosshunter.executor.sender._sleep_or_stop", side_effect=_set_event_and_fail), \
			patch("bosshunter.executor.sender.close_tab"):
			result, _ = _send_greeting_with_retry(
				job,
				"您好",
				{"send_max_retries": 3, "_workbench_stop_event": stop_event},
			)

		self.assertEqual(result["error"], "stopped")


class NoChatButtonDiagnosticsTests(unittest.TestCase):
	def test_candidates_are_surfaced_in_history_detail(self):
		"""Selector-drift diagnostics from the chat-button probe are preserved."""
		result = {
			"success": False,
			"error": "no_chat_button",
			"candidates": [
				{
					"text": "继续沟通",
					"ka": "chat_continue",
					"className": "btn continue-chat",
					"redirectUrl": None,
					"visible": True,
				}
			],
		}
		detail = _describe_no_chat_button(result)
		self.assertIn("继续沟通", detail)
		self.assertIn("chat_continue", detail)
		self.assertIn("无法找到沟通按钮", detail)

	def test_empty_candidates_gives_actionable_message(self):
		detail = _describe_no_chat_button({"success": False, "error": "no_chat_button"})
		self.assertIn("未渲染", detail)

	def test_detail_is_truncated_to_safe_length(self):
		"""Excessively verbose candidates must not bloat the history log."""
		huge = [
			{"text": "x" * 200, "ka": "k" * 100, "className": "c" * 100, "visible": False}
			for _ in range(20)
		]
		detail = _describe_no_chat_button({"success": False, "candidates": huge})
		self.assertLessEqual(len(detail), 300)

	def test_send_greeting_once_keeps_diagnostics(self):
		"""The end-to-end no_chat_button path carries the candidate detail."""
		job = {"id": "no-btn", "url": "https://www.zhipin.com/job_detail/no-btn.html"}
		# page check OK, then chat-button probe reports no_chat_button with a candidate
		candidates_payload = json.dumps(
			{
				"success": False,
				"error": "no_chat_button",
				"candidates": [
					{"text": "未知按钮", "ka": None, "className": "btn-x", "visible": True}
				],
			}
		)
		with patch("bosshunter.executor.sender.new_tab", return_value="target-1"), \
			patch("bosshunter.executor.sender.evaluate", side_effect=['{"success": true}', candidates_payload]), \
			patch("bosshunter.executor.sender.close_tab"), \
			patch("bosshunter.executor.sender.time.sleep"):
			result, target_id = _send_greeting_once(
				job, "您好", {"browse_before_greet": False, "_chat_button_attempts": 1}
			)

		self.assertFalse(result["success"])
		self.assertEqual(result["error"], "no_chat_button")
		self.assertIsNone(target_id)
		self.assertIn("未知按钮", result["history_detail"])

	def test_online_only_setting_skips_offline_hr_before_opening_chat(self):
		"""在线发送开启时，非在线 HR 不得进入聊天页或发送消息。"""
		job = {"id": "offline-hr", "url": "https://www.zhipin.com/job_detail/offline-hr.html"}
		with patch("bosshunter.executor.sender.new_tab", return_value="target-1"), \
			patch("bosshunter.executor.sender.evaluate", return_value='{"success": true, "hr_active": "本周活跃", "hr_online": false}') as evaluate, \
			patch("bosshunter.executor.sender.close_tab") as close_tab:
			result, target_id = _send_greeting_once(
				job, "您好", {"browse_before_greet": False, "only_send_when_hr_online": True}
			)

		self.assertFalse(result["success"])
		self.assertEqual(result["error"], "hr_offline")
		self.assertTrue(result["preserve_status"])
		self.assertIsNone(target_id)
		evaluate.assert_called_once()
		close_tab.assert_called_once_with("target-1")


class RecoverSendErrorsTests(unittest.TestCase):
	def _seed_job(self, db, job_id, status="error", greeting="您好"):
		insert_job(db, _job(job_id))
		update_job_status(db, job_id, status)
		update_job_greeting(db, job_id, greeting)

	def test_failed_job_outside_default_window_is_not_recovered(self):
		"""A freshly-failed job (within the backoff window) stays error."""
		with tempfile.TemporaryDirectory() as tmp:
			db = get_db(Path(tmp) / "bosshunter.db")
			try:
				self._seed_job(db, "fresh-fail")
				self.assertEqual(recover_send_errors(db, recover_after_seconds=600), 0)
				# still excluded from normal send queue
				self.assertEqual(get_jobs_ready_to_send(db), [])
			finally:
				db.close()

	def test_failed_job_past_window_is_recovered(self):
		"""A job whose updated_at is older than the window is re-queued."""
		with tempfile.TemporaryDirectory() as tmp:
			db = get_db(Path(tmp) / "bosshunter.db")
			try:
				self._seed_job(db, "stale-fail")
				# Backdate the failure so it is outside the recovery window.
				db.execute(
					"UPDATE jobs SET updated_at = datetime('now', '-1 hour') WHERE id = ?",
					("stale-fail",),
				)
				db.commit()
				recovered = recover_send_errors(db, recover_after_seconds=600)
				self.assertEqual(recovered, 1)
				ready = get_jobs_ready_to_send(db)
				self.assertEqual(len(ready), 1)
				self.assertEqual(ready[0]["id"], "stale-fail")
			finally:
				db.close()

	def test_job_exceeding_max_retries_is_not_recovered(self):
		"""Exhausting max_retries keeps a job in permanent error."""
		with tempfile.TemporaryDirectory() as tmp:
			db = get_db(Path(tmp) / "bosshunter.db")
			try:
				self._seed_job(db, "exhausted")
				# Simulate 4 prior failures (> max_retries=3) and backdate.
				db.execute(
					"UPDATE jobs SET error_retry_count = 4, "
					"updated_at = datetime('now', '-1 hour') WHERE id = ?",
					("exhausted",),
				)
				db.commit()
				self.assertEqual(recover_send_errors(db, recover_after_seconds=600, max_retries=3), 0)
				self.assertEqual(get_jobs_ready_to_send(db), [])
			finally:
				db.close()

	def test_status_with_retry_increments_then_resets(self):
		"""update_job_status_with_retry counts errors and resets on success."""
		with tempfile.TemporaryDirectory() as tmp:
			db = get_db(Path(tmp) / "bosshunter.db")
			try:
				self._seed_job(db, "counter", status="ready")
				update_job_status_with_retry(db, "counter", "error")
				cnt = db.execute(
					"SELECT error_retry_count FROM jobs WHERE id = ?", ("counter",)
				).fetchone()["error_retry_count"]
				self.assertEqual(cnt, 1)
				update_job_status_with_retry(db, "counter", "error")
				cnt = db.execute(
					"SELECT error_retry_count FROM jobs WHERE id = ?", ("counter",)
				).fetchone()["error_retry_count"]
				self.assertEqual(cnt, 2)
				# success resets the counter
				update_job_status_with_retry(db, "counter", "sent")
				row = db.execute(
					"SELECT status, error_retry_count FROM jobs WHERE id = ?", ("counter",)
				).fetchone()
				self.assertEqual(row["status"], "sent")
				self.assertEqual(row["error_retry_count"], 0)
			finally:
				db.close()


class SendMessageVerificationTests(unittest.TestCase):
	"""Cover the post-click verification added after real-DOM probing.

	Real BOSS DOM findings that motivated these tests:
	- the chat is a floating dialog (``.startchat-dialog``) on the job page,
	  not a ``/web/geek/chat`` navigation;
	- the send button is ``<div class="send-message disable">`` — BOSS marks
	  the disabled state via a class token, NOT the ``disabled`` attribute;
	- ``sendBtn.click()`` returns immediately and a click on a locked button
	  looks identical to success, so the message list must be polled.
	"""

	def test_disabled_send_button_is_detected_and_not_clicked(self):
		"""A button with class ``send-message disable`` must NOT be clicked."""
		job = {"id": "disabled-btn", "url": "https://www.zhipin.com/job_detail/disabled-btn.html"}
		evaluate_results = [
			'{"success": true}',  # page check
			json.dumps({"success": True, "action": "clicked", "button_text": "立即沟通"}),  # chat button
			'{"success": true, "action": "no_popup"}',  # popup
			'{"success": true, "path": "/web/geek/chat", "hasInput": true}',  # chat ready
			# send_msg_js: input found, but the only send button is disabled.
			# The new isDisabled() checks the class token and refuses to click.
			json.dumps({
				"success": False,
				"error": "send_button_disabled",
				"buttonClass": "send-message disable",
			}),
		]
		with patch("bosshunter.executor.sender.new_tab", return_value="target-1"), \
			patch("bosshunter.executor.sender.evaluate", side_effect=evaluate_results), \
			patch("bosshunter.executor.sender.get_page_targets", return_value=[]), \
			patch("bosshunter.executor.sender.close_tab"), \
			patch("bosshunter.executor.sender.time.sleep"):
			result, target_id = _send_greeting_once(
				job, "您好", {"browse_before_greet": False, "_chat_navigation_attempts": 1}
			)

		self.assertFalse(result["success"])
		self.assertEqual(result["error"], "send_button_disabled")
		# disabled-button failure leaves the tab open for diagnosis
		self.assertIsNotNone(target_id)

	def test_click_without_message_is_treated_as_failure(self):
		"""A click that does not produce a message bubble must NOT be a success.

	 This is the core anti-fake-success check: even if the button click
	 reports success, we poll ``.message-list`` and only succeed once a
	 message appears. Here the list stays empty → failure.
	 """
		job = {"id": "fake-success", "url": "https://www.zhipin.com/job_detail/fake-success.html"}
		evaluate_results = [
			'{"success": true}',  # page check
			json.dumps({"success": True, "action": "clicked"}),  # chat button
			'{"success": true, "action": "no_popup"}',  # popup
			'{"success": true, "path": "/web/geek/chat", "hasInput": true}',  # chat ready
			# send_msg_js: input filled, button clicked → "success:true, clicked:true"
			json.dumps({"success": True, "method": "button", "clicked": True}),
		]
		# _verify_message_sent polls; message list never grows (always count 0).
		verify_results = [
			'{"found": true, "count": 0}' for _ in range(20)
		]
		with patch("bosshunter.executor.sender.new_tab", return_value="target-1"), \
			patch("bosshunter.executor.sender.evaluate", side_effect=evaluate_results + verify_results), \
			patch("bosshunter.executor.sender.get_page_targets", return_value=[]), \
			patch("bosshunter.executor.sender.close_tab"), \
			patch("bosshunter.executor.sender.time.sleep"):
			result, target_id = _send_greeting_once(
				job, "您好", {"browse_before_greet": False, "_chat_navigation_attempts": 1}
			)

		self.assertFalse(result["success"])
		self.assertEqual(result["error"], "send_verify_failed")
		self.assertIn("未出现", result["history_detail"])

	def test_verified_message_succeeds(self):
		"""When the message list grows after the click, the send is real."""
		job = {"id": "real-send", "url": "https://www.zhipin.com/job_detail/real-send.html"}
		evaluate_results = [
			'{"success": true}',  # page check
			json.dumps({"success": True, "action": "clicked"}),  # chat button
			'{"success": true, "action": "no_popup"}',  # popup
			'{"success": true, "path": "/web/geek/chat", "hasInput": true}',  # chat ready
			json.dumps({"success": True, "method": "button", "clicked": True}),  # send click
			# first verify poll already sees the message bubble
			'{"found": true, "count": 1}',
		]
		with patch("bosshunter.executor.sender.new_tab", return_value="target-1"), \
			patch("bosshunter.executor.sender.evaluate", side_effect=evaluate_results), \
			patch("bosshunter.executor.sender.get_page_targets", return_value=[]), \
			patch("bosshunter.executor.sender.close_tab") as close_tab, \
			patch("bosshunter.executor.sender.time.sleep"):
			result, target_id = _send_greeting_once(
				job, "您好", {"browse_before_greet": False, "_chat_navigation_attempts": 1}
			)

		self.assertTrue(result["success"])
		self.assertIsNone(target_id)
		close_tab.assert_called_once_with("target-1")

	def test_verify_message_sent_times_out_when_list_missing(self):
		"""No message-list container at all → verification fails."""
		with patch("bosshunter.executor.sender.evaluate", return_value='{"found": false, "count": -1}'), \
			patch("bosshunter.executor.sender.time.sleep"):
			result = _verify_message_sent("target-1", None, attempts=2)

		self.assertFalse(result["success"])
		self.assertEqual(result["error"], "send_verify_failed")

	def test_verify_message_sent_succeeds_when_message_appears(self):
		"""The list growing from 0 to 1 message proves delivery."""
		with patch(
			"bosshunter.executor.sender.evaluate",
			side_effect=['{"found": true, "count": 0}', '{"found": true, "count": 1}'],
		), patch("bosshunter.executor.sender.time.sleep"):
			result = _verify_message_sent("target-1", None, attempts=5)

		self.assertTrue(result["success"])
		self.assertEqual(result["message_count"], 1)


if __name__ == "__main__":
	unittest.main()
