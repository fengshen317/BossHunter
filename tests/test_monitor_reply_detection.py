"""Regression tests for HR-reply detection in the monitor.

These guard the core fix for a bug where the user's OWN outgoing messages
(greeting / reply) were being mis-detected as "HR 回复":

1. ``check_replies`` must NOT flip a tracked job from ``sent`` to ``replied``
   based solely on the chat-list preview text, and must NOT write a fake
   ``"HR回复: <预览文本>"`` history row. The preview is "the last message in
   the conversation" — which is usually the user's own outgoing message.
2. The ``sent → replied`` status advance may only happen after the full
   conversation is opened and ``_handle_conversation`` confirms there really
   is an HR message the user has not yet replied to.
3. The chat-list JS must keep ``needsInspection`` permissive: the preview
   stage cannot reliably distinguish "I sent & it's read" from "HR replied
   & I read it but haven't answered", so it must NOT pre-filter on
   ``isOurMessage``. Reliable filtering happens downstream in
   ``_handle_conversation`` via ``.item-myself``/``.item-friend`` and the
   "have I already replied" check.
"""

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


class ChatListJSTests(unittest.TestCase):
	def test_needs_inspection_stays_permissive_and_drops_unreliable_filter(self):
		"""列表预览阶段无法可靠区分“我发的”还是“HR 发的、我已读未回”，
		所以 needsInspection 必须保持宽松（仅排除系统占位文案），不能用
		isOurMessage 做预过滤——否则会把 HR 真回复（已读未回）也过滤掉。
		真正的归属/已回复判断交给 _handle_conversation 打开完整会话后做。
		"""
		from bosshunter.executor import monitor

		js = monitor.JS_EXTRACT_CHAT_LIST
		# 不得再用 confidentOurs 这类基于不可靠发送态的预过滤
		self.assertNotIn("confidentOurs", js)
		self.assertNotIn("!confidentOurs", js)
		# 仍然排除系统占位文案（这个排除是安全的）
		self.assertIn("isSystemHint", js)

	def test_is_our_message_recognizes_status_sent_class_as_diagnostic(self):
		"""is_our_message 字段保留增强识别（status-sent），仅用于诊断，不参与过滤。"""
		from bosshunter.executor import monitor

		self.assertIn("status-sent", monitor.JS_EXTRACT_CHAT_LIST)
		# 原有的 read/delivery 标记仍然保留
		self.assertIn("status-read", monitor.JS_EXTRACT_CHAT_LIST)
		self.assertIn("status-delivery", monitor.JS_EXTRACT_CHAT_LIST)


class CheckRepliesNoPrematureStatusChangeTests(unittest.TestCase):
	"""check_replies 只收集候选，不得基于预览改状态/写 HR 回复历史。"""

	def _db(self, tmp: str):
		return get_db(Path(tmp) / "data" / "bosshunter.db")

	def _conv(self, *, last_message: str, is_our_message: bool, unread_count: int = 0):
		return {
			"hr_name": "李主管",
			"company": "Example",
			"hr_title": "HR",
			"last_message": last_message,
			"has_reply": not is_our_message,
			"needs_inspection": True,  # 即使进入了候选集，也不应改状态
			"unread_count": unread_count,
			"is_our_message": is_our_message,
			"conversation_id": "conv-self",
			"element_index": 0,
		}

	def test_self_sent_preview_does_not_advance_sent_to_replied(self):
		"""预览文本是“我发的招呼”时，不得把 sent→replied，也不得写 HR回复 历史。"""
		from bosshunter.executor import monitor

		# 预览就是用户自己发出的招呼语——这正是 bug 触发场景。
		self_sent_preview = self._conv(
			last_message="您好，我对这个岗位很感兴趣，希望能聊聊",
			is_our_message=True,
		)

		with tempfile.TemporaryDirectory() as tmp:
			db_path = Path(tmp) / "data" / "bosshunter.db"
			db = self._db(tmp)
			try:
				insert_job(db, _job("job-self"))
				update_job_status(db, "job-self", "sent")
			finally:
				db.close()

			def open_db():
				return get_db(db_path)

			with patch.object(monitor, "get_db", side_effect=open_db), \
				patch.object(monitor, "new_tab", return_value="target-1"), \
				patch.object(monitor, "wait_for_load", return_value=True), \
				patch.object(
					monitor, "evaluate",
					return_value=json.dumps([self_sent_preview], ensure_ascii=False),
				), \
				patch.object(monitor, "close_tab"), \
				patch.object(monitor, "time") as mock_time:
				mock_time.sleep.return_value = None
				results = monitor.check_replies({"monitor": {}})

			# 候选仍然返回（交给 _handle_conversation 核验），但状态/历史不得变。
			verify_db = get_db(db_path)
			try:
				row = verify_db.execute(
					"SELECT status FROM jobs WHERE id = ?", ("job-self",)
				).fetchone()
				history = [
					r["action"]
					for r in verify_db.execute(
						"SELECT action FROM history WHERE job_id = ?", ("job-self",)
					).fetchall()
				]
			finally:
				verify_db.close()

			self.assertEqual(len(results), 1)
			self.assertEqual(row["status"], "sent", "不得基于预览把 sent 推进到 replied")
			self.assertEqual(history, [], "不得基于预览写任何 HR回复 历史")


class ChatListCandidateScopeTests(unittest.TestCase):
	"""列表候选筛选的边界——宽松但安全。

	核心教训：列表预览阶段无法可靠区分“我发的已读”和“HR 发的、我已读未回”，
	所以 needsInspection 必须宽松（宁多核验），把误判留给下游 _handle_conversation。
	这里锁定两类关键场景不被预过滤掉。
	"""

	def test_hr_reply_already_read_but_unanswered_stays_in_candidates(self):
		"""HR 真回复、用户已读、尚未回复的会话（unread=0）必须保留在候选里。

		这是“监测不到 HR 回复”回归的根因：之前用 isOurMessage && unreadCount===0
		做预过滤，而 status-read 既可能是本人消息也可能是“我读过对方消息”，
		导致 HR 已读未回的真回复被跳过。
		"""
		from bosshunter.executor import monitor

		# needsInspection = !!lastMessage && !isSystemHint —— 不含 !confidentOurs
		js = monitor.JS_EXTRACT_CHAT_LIST
		# 提取 needsInspection 赋值行，确认它只依赖 lastMessage 和 isSystemHint
		# 不再依赖 isOurMessage / unreadCount（这两个在预览阶段不可靠）。
		self.assertIn("needsInspection = !!lastMessage && !isSystemHint", js)
		self.assertNotIn("confidentOurs", js)

	def test_system_placeholder_hint_is_excluded(self):
		"""系统占位文案“您正在与BossXX沟通”仍要排除（它不是真实消息）。"""
		from bosshunter.executor import monitor

		self.assertIn("isSystemHint", monitor.JS_EXTRACT_CHAT_LIST)
		self.assertIn("您正在与Boss", monitor.JS_EXTRACT_CHAT_LIST)


class MonitorCycleStatusAdvanceTests(unittest.TestCase):
	"""monitor_and_send_resumes 才是 sent→replied 的唯一推进点，且仅在确认 HR 待回复后。"""

	def _db(self, tmp: str):
		return get_db(Path(tmp) / "data" / "bosshunter.db")

	def _patch_browser_window(self):
		# SendWindowChecker 默认窗口 09:00-16:00 在 CI 夜晚会跳过整个循环。
		return patch(
			"bosshunter.executor.monitor.SendWindowChecker.is_active",
			return_value=True,
		)

	def test_real_hr_pending_reply_advances_sent_to_replied(self):
		"""完整会话核验出真 HR 待回复（reply_pending）→ 推进 sent→replied。"""
		from bosshunter.executor import monitor

		job = _job("job-real") | {"status": "sent"}
		with tempfile.TemporaryDirectory() as tmp:
			db_path = Path(tmp) / "data" / "bosshunter.db"
			db = self._db(tmp)
			try:
				insert_job(db, _job("job-real"))
				update_job_status(db, "job-real", "sent")
			finally:
				db.close()

			def open_db():
				return get_db(db_path)

			with self._patch_browser_window(), \
				patch.object(monitor, "check_replies", return_value=[{"job": job, "conversation": {}}]), \
				patch.object(monitor, "_handle_conversation", return_value="reply_pending"), \
				patch.object(monitor, "_check_follow_ups", return_value=0), \
				patch.object(monitor, "get_db", side_effect=open_db):
				summary = monitor.monitor_and_send_resumes({"throttle": {}})

			verify_db = get_db(db_path)
			try:
				status = verify_db.execute(
					"SELECT status FROM jobs WHERE id = ?", ("job-real",)
				).fetchone()["status"]
			finally:
				verify_db.close()

			self.assertEqual(summary["pending"], 1)
			self.assertEqual(status, "replied")

	def test_skipped_user_replied_does_not_advance_sent_to_replied(self):
		"""最后一条是我发的（skipped_user_replied）→ 保持 sent，绝不推进。"""
		from bosshunter.executor import monitor

		job = _job("job-skip") | {"status": "sent"}
		with tempfile.TemporaryDirectory() as tmp:
			db_path = Path(tmp) / "data" / "bosshunter.db"
			db = self._db(tmp)
			try:
				insert_job(db, _job("job-skip"))
				update_job_status(db, "job-skip", "sent")
			finally:
				db.close()

			def open_db():
				return get_db(db_path)

			with self._patch_browser_window(), \
				patch.object(monitor, "check_replies", return_value=[{"job": job, "conversation": {}}]), \
				patch.object(monitor, "_handle_conversation", return_value="skipped_user_replied"), \
				patch.object(monitor, "_check_follow_ups", return_value=0), \
				patch.object(monitor, "get_db", side_effect=open_db):
				summary = monitor.monitor_and_send_resumes({"throttle": {}})

			verify_db = get_db(db_path)
			try:
				status = verify_db.execute(
					"SELECT status FROM jobs WHERE id = ?", ("job-skip",)
				).fetchone()["status"]
			finally:
				verify_db.close()

			self.assertEqual(summary["skipped"], 1)
			self.assertEqual(status, "sent", "我已回复的会话不得推进到 replied")

	def test_already_replied_status_is_not_downgraded_or_re_advanced(self):
		"""已处于 replied 的岗位再次命中时，状态保持 replied，不重复触发副作用。"""
		from bosshunter.executor import monitor

		job = _job("job-done") | {"status": "replied"}
		with tempfile.TemporaryDirectory() as tmp:
			db_path = Path(tmp) / "data" / "bosshunter.db"
			db = self._db(tmp)
			try:
				insert_job(db, _job("job-done"))
				update_job_status(db, "job-done", "replied")
			finally:
				db.close()

			def open_db():
				return get_db(db_path)

			with self._patch_browser_window(), \
				patch.object(monitor, "check_replies", return_value=[{"job": job, "conversation": {}}]), \
				patch.object(monitor, "_handle_conversation", return_value="reply_pending"), \
				patch.object(monitor, "_check_follow_ups", return_value=0), \
				patch.object(monitor, "get_db", side_effect=open_db):
				summary = monitor.monitor_and_send_resumes({"throttle": {}})

			verify_db = get_db(db_path)
			try:
				status = verify_db.execute(
					"SELECT status FROM jobs WHERE id = ?", ("job-done",)
				).fetchone()["status"]
			finally:
				verify_db.close()

			# 推进条件要求 prior_status == 'sent'；已是 replied 不应被反复写。
			self.assertEqual(summary["pending"], 1)
			self.assertEqual(status, "replied")


class HandleConversationReadButUnansweredTests(unittest.TestCase):
	"""_handle_conversation 必须处理“HR 已回复、我已读但未回复”的会话。

	这是用户最核心的诉求：HR 回复了（哪怕我点开看过了、未读徽标已清），
	只要我还没回复，监测就必须生成回复建议，不能跳过。
	已读与否不影响判断——只看“我最后回复之后有没有新的 HR 消息”。
	"""

	def test_hr_message_after_my_greeting_generates_reply_even_when_read(self):
		"""HR 在我的招呼后回了消息（我已读未回）→ 必须生成回复，不得跳过。"""
		from bosshunter.executor import monitor

		# 我发了招呼 → HR 回复 → 我已读但没回（unread 徽标已清，但最后一条是 HR）
		messages = [
			{"sender": "me", "text": "您好，我对岗位很感兴趣。"},
			{"sender": "hr", "text": "方便发一份你的简历吗？顺便说说你的优势。"},
		]

		with tempfile.TemporaryDirectory() as tmp:
			db_path = Path(tmp) / "data" / "bosshunter.db"
			db = get_db(db_path)
			try:
				insert_job(db, _job("job-read-unanswered"))
				update_job_status(db, "job-read-unanswered", "sent")
			finally:
				db.close()

			def open_db():
				return get_db(db_path)

			with patch.object(monitor, "get_db", side_effect=open_db), \
				patch.object(monitor, "_open_conversation", return_value="target-1"), \
				patch.object(monitor, "evaluate", return_value=json.dumps(messages, ensure_ascii=False)), \
				patch.object(monitor, "close_tab"), \
				patch.object(monitor.time, "sleep"), \
				patch.object(monitor, "_generate_auto_reply", return_value="可以的，我整理后发您。") as gen:
				action = monitor._handle_conversation(
					_job("job-read-unanswered") | {"status": "sent"},
					{"monitor": {}},
				)

		# 必须走到生成回复分支，而不是 skipped_user_replied
		self.assertEqual(action, "reply_pending")
		gen.assert_called_once()

	def test_no_hr_message_after_my_reply_is_skipped(self):
		"""对照：最后一条是我发的（我回复了 HR、HR 没再回）→ 跳过，不生成。"""
		from bosshunter.executor import monitor

		messages = [
			{"sender": "hr", "text": "方便聊聊吗？"},
			{"sender": "me", "text": "可以的，您说。"},
		]

		with tempfile.TemporaryDirectory() as tmp:
			db_path = Path(tmp) / "data" / "bosshunter.db"
			db = get_db(db_path)
			try:
				insert_job(db, _job("job-already-replied"))
				update_job_status(db, "job-already-replied", "sent")
			finally:
				db.close()

			def open_db():
				return get_db(db_path)

			with patch.object(monitor, "get_db", side_effect=open_db), \
				patch.object(monitor, "_open_conversation", return_value="target-1"), \
				patch.object(monitor, "evaluate", return_value=json.dumps(messages, ensure_ascii=False)), \
				patch.object(monitor, "close_tab"), \
				patch.object(monitor.time, "sleep"), \
				patch.object(monitor, "_generate_auto_reply") as gen:
				action = monitor._handle_conversation(
					_job("job-already-replied") | {"status": "sent"},
					{"monitor": {}},
				)

		self.assertEqual(action, "skipped_user_replied")
		gen.assert_not_called()


if __name__ == "__main__":
	unittest.main()
