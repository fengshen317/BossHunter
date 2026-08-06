import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bosshunter.db import get_db, insert_job
from bosshunter.executor import monitor


def _job(job_id: str = "job-1") -> dict:
    """构造监控定位测试使用的岗位。"""
    return {
        "id": job_id,
        "title": "AI应用工程师",
        "company": "示例科技",
        "salary": "20-30K",
        "city": "深圳",
        "experience": "3-5年",
        "jd": "负责 AI 应用开发",
        "hr_name": "杨女士",
        "hr_title": "HRBP",
        "hr_active": "",
        "company_size": "",
        "company_industry": "",
        "url": f"https://www.zhipin.com/job_detail/{job_id}.html",
    }


class MonitorConversationNavigationTests(unittest.TestCase):
    def test_chat_list_uses_real_unread_badge_and_ignores_system_hint(self):
        """列表扫描应识别真实未读徽标，并排除“正在沟通”系统占位文案。"""
        self.assertIn(".notice-badge", monitor.JS_EXTRACT_CHAT_LIST)
        self.assertIn("unread_count", monitor.JS_EXTRACT_CHAT_LIST)
        self.assertIn("isSystemHint", monitor.JS_EXTRACT_CHAT_LIST)
        self.assertIn("needs_inspection", monitor.JS_EXTRACT_CHAT_LIST)

    def test_conversation_extraction_reads_each_message_container_once(self):
        """正文与嵌套卡片不能被父子节点重复拼接。"""
        self.assertIn("if (primaryText) return primaryText", monitor.JS_EXTRACT_CONVERSATION)
        # 去重方向：丢弃“内部还嵌套消息节点”的容器（如 .chat-message 包着
        # .message-item），而不是丢弃嵌套在容器里的叶子消息。旧逻辑查
        # parentElement.closest 方向反了，会把所有真消息过滤掉、只剩容器。
        self.assertIn("!msg.querySelector(MESSAGE_SELECTORS)", monitor.JS_EXTRACT_CONVERSATION)
        self.assertNotIn("card.querySelectorAll(ACTION_SELECTORS)", monitor.JS_EXTRACT_CONVERSATION)

    def test_conversation_extraction_resolves_sender_from_ancestor_without_defaulting_to_hr(self):
        """正文在内层时应读取外层归属标记，未知结构也不能误报为 HR。"""
        script = monitor.JS_EXTRACT_CONVERSATION

        self.assertIn("msg.closest(ownerSelectors)", script)
        self.assertIn("item-myself", script)
        self.assertIn("item-friend", script)
        self.assertIn("(isHr ? 'hr' : 'unknown')", script)
        self.assertNotIn("(isMe ? 'me' : 'hr')", script)

    def test_conversation_extraction_drops_wrapper_not_nested_leaf_messages(self):
        """回归：BOSS 真实 DOM 里 .chat-message 是容器（包着若干 .message-item），
        不是单条消息。去重必须丢弃容器、保留叶子消息。

        旧逻辑查 ``parentElement.closest(MESSAGE_SELECTORS)``——方向反了：
        .message-item 的父级 .im-list 向上能命中祖先 .chat-message，导致所有
        真消息都被过滤掉，只剩 .chat-message 容器；容器再靠 querySelector
        命中第一个 .item-friend 子节点，把整段（含本人招呼语）全标成 hr，
        于是“我发的消息”被当成“HR 回复”生成了回复建议。
        """
        script = monitor.JS_EXTRACT_CONVERSATION

        # 去重条件必须是“内部不含其它消息节点”（丢弃容器），而非“父链上有消息节点”（丢弃叶子）
        self.assertIn("!msg.querySelector(MESSAGE_SELECTORS)", script)
        # 绝不能保留旧的、方向相反的过滤
        self.assertNotIn("!msg.parentElement.closest(MESSAGE_SELECTORS)", script)
        # 真实 BOSS DOM 里 .message-item 直接带 item-myself/item-friend，归属靠自身 class
        self.assertIn("item-myself", script)
        self.assertIn("item-friend", script)

    def test_adjacent_duplicate_dom_messages_are_collapsed(self):
        """DOM 同一消息被选择器命中两次时只保留一条。"""
        messages = [
            {"sender": "hr", "kind": "message", "text": "方便聊聊吗"},
            {"sender": "hr", "kind": "message", "text": " 方便聊聊吗 "},
            {"sender": "system", "kind": "message", "text": "平台卡片"},
            {"sender": "hr", "kind": "message", "text": "方便聊聊吗"},
        ]

        result = monitor._deduplicate_conversation_messages(messages)

        self.assertEqual(len(result), 3)
        self.assertEqual([item["sender"] for item in result], ["hr", "system", "hr"])

    def test_saved_conversation_id_opens_exact_chat_directly(self):
        """已有稳定会话标识时不应再从聊天列表猜测联系人。"""
        job = _job() | {"conversation_id": "conversation-1"}

        with patch.object(monitor, "new_tab", return_value="chat-target") as new_tab_mock, \
             patch.object(monitor, "_wait_for_conversation_ready", return_value=True), \
             patch.object(monitor, "_open_conversation_from_chat_list") as list_fallback:
            target_id = monitor._open_conversation(job, {})

        self.assertEqual(target_id, "chat-target")
        new_tab_mock.assert_called_once_with(
            "https://www.zhipin.com/web/geek/chat?id=conversation-1&jobId=job-1"
        )
        list_fallback.assert_not_called()

    def test_job_page_redirect_backfills_conversation_id(self):
        """旧岗位应从继续沟通按钮恢复稳定会话标识并写回数据库。"""
        job = _job("legacy-job")
        redirect_url = "/web/geek/chat?id=restored-id&jobId=legacy-job&securityId=temporary"

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "bosshunter.db"
            db = get_db(db_path)
            try:
                insert_job(db, job)
            finally:
                db.close()

            def open_db():
                return get_db(db_path)

            with patch.object(monitor, "new_tab", return_value="job-target"), \
                 patch.object(monitor, "wait_for_load"), \
                 patch.object(monitor.time, "sleep"), \
                 patch.object(
                     monitor,
                     "evaluate",
                     return_value={
                         "success": True,
                         "button_text": "继续沟通",
                         "redirect_url": redirect_url,
                     },
                 ), \
                 patch.object(monitor, "navigate", return_value=True) as navigate_mock, \
                 patch.object(monitor, "_wait_for_conversation_ready", return_value=True), \
                 patch.object(monitor, "get_db", side_effect=open_db):
                target_id = monitor._open_conversation(job, {})

            verify_db = get_db(db_path)
            try:
                conversation_id = verify_db.execute(
                    "SELECT conversation_id FROM jobs WHERE id = ?",
                    (job["id"],),
                ).fetchone()["conversation_id"]
            finally:
                verify_db.close()

        self.assertEqual(target_id, "job-target")
        self.assertEqual(conversation_id, "restored-id")
        navigate_mock.assert_called_once_with(
            "job-target",
            "https://www.zhipin.com/web/geek/chat?id=restored-id&jobId=legacy-job",
        )

    def test_chat_list_match_requires_visible_input_and_selected_contact(self):
        """列表点击后必须验证输入框和选中的 HR，防止串到其他会话。"""
        mismatch_state = {
            "success": True,
            "input_visible": True,
            "selected_text": "其他招聘者 另一家公司",
        }

        self.assertFalse(
            monitor._conversation_state_matches_job(mismatch_state, _job())
        )


if __name__ == "__main__":
    unittest.main()
