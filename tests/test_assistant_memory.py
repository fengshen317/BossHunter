"""版本化长期记忆的回归测试。"""

import sqlite3

from bosshunter.assistant.repository import (
	edit_memory,
	get_conversation_context,
	init_assistant_tables,
	list_message_page,
	list_memories,
	restore_memory,
	save_pending_intent,
	search_memories,
	upsert_memory,
)
from bosshunter.assistant.memory import normalize_memory_operations


def _db() -> sqlite3.Connection:
	"""创建隔离的内存数据库。"""
	conn = sqlite3.connect(":memory:")
	conn.row_factory = sqlite3.Row
	init_assistant_tables(conn)
	return conn


def test_memory_conflict_creates_auditable_version() -> None:
	"""同键新值应替换旧版本，而不是留下两个生效事实。"""
	conn = _db()
	try:
		first = upsert_memory(
			conn,
			memory_key="preference.target_city",
			category="preference",
			content="用户优先考虑北京岗位",
			value=["北京"],
			source_type="automatic",
			confidence=0.95,
		)
		second = upsert_memory(
			conn,
			memory_key="preference.target_city",
			category="preference",
			content="用户优先考虑上海岗位",
			value=["上海"],
			source_type="automatic",
			confidence=0.96,
		)

		active = list_memories(conn)
		history = list_memories(conn, status="history")
	finally:
		conn.close()

	assert [item["content"] for item in active] == ["用户优先考虑上海岗位"]
	assert second["supersedes_id"] == first["id"]
	assert {item["status"] for item in history} == {"active", "superseded"}


def test_memory_same_value_is_deduplicated() -> None:
	"""相同键值重复出现时只更新来源，不产生新版本。"""
	conn = _db()
	try:
		first = upsert_memory(
			conn,
			memory_key="workflow.answer_language",
			category="workflow",
			content="使用中文回答",
			value="中文",
			source_type="automatic",
			confidence=0.9,
		)
		second = upsert_memory(
			conn,
			memory_key="workflow.answer_language",
			category="workflow",
			content="使用中文回答",
			value="中文",
			source_type="automatic",
			confidence=0.95,
		)
		items = list_memories(conn, status="history")
	finally:
		conn.close()

	assert first["id"] == second["id"]
	assert len(items) == 1


def test_chinese_memory_search_has_no_unrelated_recent_fallback() -> None:
	"""中文问题应命中相关记忆，无命中时不得返回最近的不相关内容。"""
	conn = _db()
	try:
		upsert_memory(
			conn,
			memory_key="preference.target_city",
			category="preference",
			content="用户优先考虑深圳岗位",
			value=["深圳"],
			source_type="automatic",
			confidence=0.95,
		)
		matched = search_memories(conn, "我之前优先考虑哪个城市")
		unmatched = search_memories(conn, "今天应该学习什么算法")
	finally:
		conn.close()

	assert matched and "深圳" in matched[0]["content"]
	assert unmatched == []


def test_memory_candidate_must_quote_user_and_pass_sensitive_filter() -> None:
	"""自动记忆必须回指用户原文，并由程序拒绝敏感信息。"""
	user_text = "我以后优先考虑深圳岗位，API Key: secret-value"
	operations = normalize_memory_operations(
		[
			{
				"operation": "upsert",
				"memory_key": "preference.target_city",
				"category": "preference",
				"content": "用户优先考虑深圳岗位",
				"value": ["深圳"],
				"source_excerpt": "我以后优先考虑深圳岗位",
				"confidence": 0.95,
			},
			{
				"operation": "upsert",
				"memory_key": "profile.api_key",
				"category": "profile",
				"content": "API Key: secret-value",
				"value": "secret-value",
				"source_excerpt": "API Key: secret-value",
				"confidence": 0.99,
			},
			{
				"operation": "upsert",
				"memory_key": "profile.inferred_skill",
				"category": "profile",
				"content": "用户精通 Rust",
				"value": "Rust",
				"source_excerpt": "用户精通 Rust",
				"confidence": 0.99,
			},
		],
		user_text,
	)

	assert [item["memory_key"] for item in operations] == ["preference.target_city"]


def test_edit_and_restore_memory_keep_version_history() -> None:
	"""编辑和恢复都应创建新版本，不能覆盖审计历史。"""
	conn = _db()
	try:
		first = upsert_memory(
			conn,
			memory_key="preference.target_city",
			category="preference",
			content="用户优先考虑深圳岗位",
			value="深圳",
			source_type="automatic",
			confidence=0.95,
		)
		edited = edit_memory(conn, first["id"], "用户优先考虑上海岗位")
		assert edited is not None
		restored = restore_memory(conn, first["id"])
		active = list_memories(conn)
		history = list_memories(conn, status="history")
	finally:
		conn.close()

	assert restored is not None
	assert active[0]["content"] == "用户优先考虑深圳岗位"
	assert len(history) == 3
	assert sum(item["status"] == "active" for item in history) == 1


def test_pending_intent_can_be_saved_and_cleared() -> None:
	"""待澄清意图应跨 run 持久化，并可在完成后清空。"""
	conn = _db()
	try:
		save_pending_intent(conn, "conversation-1", {"intent_type": "search_jobs", "missing_fields": ["city"]})
		pending = get_conversation_context(conn, "conversation-1")["pending_intent"]
		save_pending_intent(conn, "conversation-1", None)
		cleared = get_conversation_context(conn, "conversation-1")["pending_intent"]
	finally:
		conn.close()

	assert pending["missing_fields"] == ["city"]
	assert cleared == {}


def test_message_page_uses_stable_cursor_without_duplicates() -> None:
	"""消息分页应从最新页向前翻页，并保持每页从旧到新。"""
	from bosshunter.assistant.repository import add_message, create_conversation

	conn = _db()
	try:
		conversation = create_conversation(conn, "分页测试")
		for index in range(7):
			add_message(conn, conversation["id"], "user", f"消息 {index}")
		latest = list_message_page(conn, conversation["id"], limit=3)
		older = list_message_page(conn, conversation["id"], before=latest["next_cursor"], limit=3)
		oldest = list_message_page(conn, conversation["id"], before=older["next_cursor"], limit=3)
	finally:
		conn.close()

	assert latest["has_more"] is True
	assert set(item["id"] for item in latest["items"]).isdisjoint(item["id"] for item in older["items"])
	all_items = oldest["items"] + older["items"] + latest["items"]
	assert {item["content"] for item in all_items} == {f"消息 {index}" for index in range(7)}
	assert [(item["created_at"], item["id"]) for item in all_items] == sorted(
		(item["created_at"], item["id"]) for item in all_items
	)
