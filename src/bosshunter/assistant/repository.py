"""AI 助手的会话、消息与长期记忆持久化。"""

import json
import sqlite3
import uuid
from typing import Any


def init_assistant_tables(conn: sqlite3.Connection) -> None:
	"""创建 AI 助手使用的业务表。

	Args:
		conn: BossHunter 业务数据库连接。
	"""
	conn.executescript("""
		CREATE TABLE IF NOT EXISTS assistant_conversations (
			id TEXT PRIMARY KEY,
			title TEXT NOT NULL,
			created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
			updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
		);

		CREATE TABLE IF NOT EXISTS assistant_messages (
			id TEXT PRIMARY KEY,
			conversation_id TEXT NOT NULL,
			role TEXT NOT NULL,
			content TEXT NOT NULL,
			message_type TEXT NOT NULL DEFAULT 'text',
			metadata_json TEXT NOT NULL DEFAULT '{}',
			created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
			FOREIGN KEY (conversation_id) REFERENCES assistant_conversations(id)
		);

		CREATE TABLE IF NOT EXISTS assistant_memories (
			id TEXT PRIMARY KEY,
			user_id TEXT NOT NULL,
			memory_type TEXT NOT NULL,
			content TEXT NOT NULL,
			metadata_json TEXT NOT NULL DEFAULT '{}',
			confidence REAL NOT NULL DEFAULT 1.0,
			status TEXT NOT NULL DEFAULT 'active',
			created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
			updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
		);

		CREATE TABLE IF NOT EXISTS assistant_bad_cases (
			id TEXT PRIMARY KEY,
			conversation_id TEXT NOT NULL,
			assistant_message_id TEXT NOT NULL UNIQUE,
			user_message_id TEXT,
			category TEXT NOT NULL,
			expected_intent TEXT NOT NULL DEFAULT '',
			feedback TEXT NOT NULL DEFAULT '',
			user_message TEXT NOT NULL DEFAULT '',
			assistant_answer TEXT NOT NULL,
			intent_json TEXT NOT NULL DEFAULT '{}',
			routing_json TEXT NOT NULL DEFAULT '{}',
			status TEXT NOT NULL DEFAULT 'open',
			created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
			updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
			FOREIGN KEY (conversation_id) REFERENCES assistant_conversations(id),
			FOREIGN KEY (assistant_message_id) REFERENCES assistant_messages(id)
		);

		CREATE TABLE IF NOT EXISTS assistant_message_ratings (
			assistant_message_id TEXT PRIMARY KEY,
			conversation_id TEXT NOT NULL,
			rating TEXT NOT NULL,
			created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
			updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
			FOREIGN KEY (assistant_message_id) REFERENCES assistant_messages(id),
			FOREIGN KEY (conversation_id) REFERENCES assistant_conversations(id)
		);

		CREATE INDEX IF NOT EXISTS idx_assistant_messages_conversation
		ON assistant_messages(conversation_id, created_at);
		CREATE INDEX IF NOT EXISTS idx_assistant_memories_user
		ON assistant_memories(user_id, status, updated_at);
		CREATE INDEX IF NOT EXISTS idx_assistant_bad_cases_status
		ON assistant_bad_cases(status, created_at);
	""")
	conn.commit()


def create_conversation(conn: sqlite3.Connection, title: str = "新的求职对话") -> dict[str, Any]:
	"""创建一个聊天会话。

	Args:
		conn: 业务数据库连接。
		title: 会话标题。

	Returns:
		新会话记录。
	"""
	init_assistant_tables(conn)
	conversation_id = str(uuid.uuid4())
	conn.execute(
		"INSERT INTO assistant_conversations (id, title) VALUES (?, ?)",
		(conversation_id, title[:60] or "新的求职对话"),
	)
	conn.commit()
	return {"id": conversation_id, "title": title[:60] or "新的求职对话"}


def list_conversations(conn: sqlite3.Connection) -> list[dict[str, Any]]:
	"""按最近更新时间列出会话。

	Args:
		conn: 业务数据库连接。

	Returns:
		会话列表。
	"""
	init_assistant_tables(conn)
	rows = conn.execute("""
		SELECT c.id, c.title, c.created_at, c.updated_at,
			(SELECT content FROM assistant_messages m
			 WHERE m.conversation_id = c.id ORDER BY m.created_at DESC, m.rowid DESC LIMIT 1) AS preview
		FROM assistant_conversations c
		ORDER BY c.updated_at DESC
	""").fetchall()
	return [dict(row) for row in rows]


def delete_conversations(conn: sqlite3.Connection, conversation_ids: list[str]) -> int:
	"""删除指定对话及其消息，返回实际删除的对话数量。"""
	if not conversation_ids:
		return 0
	placeholders = ",".join("?" for _ in conversation_ids)
	conn.execute(f"DELETE FROM assistant_messages WHERE conversation_id IN ({placeholders})", conversation_ids)
	result = conn.execute(f"DELETE FROM assistant_conversations WHERE id IN ({placeholders})", conversation_ids)
	conn.commit()
	return result.rowcount


def add_message(
	conn: sqlite3.Connection,
	conversation_id: str,
	role: str,
	content: str,
	message_type: str = "text",
	metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
	"""保存聊天消息并更新会话时间。

	Args:
		conn: 业务数据库连接。
		conversation_id: 会话 ID。
		role: user 或 assistant。
		content: 消息正文。
		message_type: 文本或审批卡片类型。
		metadata: 引用、待确认动作等结构化信息。

	Returns:
		保存后的消息。
	"""
	init_assistant_tables(conn)
	message_id = str(uuid.uuid4())
	conn.execute(
		"""INSERT INTO assistant_messages
		(id, conversation_id, role, content, message_type, metadata_json)
		VALUES (?, ?, ?, ?, ?, ?)""",
		(message_id, conversation_id, role, content, message_type, json.dumps(metadata or {}, ensure_ascii=False)),
	)
	conn.execute(
		"UPDATE assistant_conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
		(conversation_id,),
	)
	if role == "user":
		row = conn.execute(
			"SELECT COUNT(*) AS count FROM assistant_messages WHERE conversation_id = ?",
			(conversation_id,),
		).fetchone()
		if row and row["count"] == 1:
			conn.execute(
				"UPDATE assistant_conversations SET title = ? WHERE id = ?",
				(content.strip().replace("\n", " ")[:32] or "新的求职对话", conversation_id),
			)
	conn.commit()
	return {
		"id": message_id,
		"conversation_id": conversation_id,
		"role": role,
		"content": content,
		"message_type": message_type,
		"metadata": metadata or {},
	}


def list_messages(conn: sqlite3.Connection, conversation_id: str) -> list[dict[str, Any]]:
	"""列出会话消息。

	Args:
		conn: 业务数据库连接。
		conversation_id: 会话 ID。

	Returns:
		按时间正序排列的消息。
	"""
	init_assistant_tables(conn)
	rows = conn.execute(
		"""SELECT id, conversation_id, role, content, message_type, metadata_json, created_at
		FROM assistant_messages WHERE conversation_id = ?
		ORDER BY created_at, rowid""",
		(conversation_id,),
	).fetchall()
	result = []
	for row in rows:
		item = dict(row)
		try:
			item["metadata"] = json.loads(item.pop("metadata_json"))
		except (json.JSONDecodeError, TypeError):
			item["metadata"] = {}
		result.append(item)
	return result


def remember(conn: sqlite3.Connection, content: str, user_id: str = "local") -> dict[str, Any]:
	"""保存用户明确要求记住的信息。

	Args:
		conn: 业务数据库连接。
		content: 记忆正文。
		user_id: 本地用户命名空间。

	Returns:
		新记忆记录。
	"""
	init_assistant_tables(conn)
	memory_id = str(uuid.uuid4())
	conn.execute(
		"""INSERT INTO assistant_memories
		(id, user_id, memory_type, content, metadata_json)
		VALUES (?, ?, 'explicit', ?, ?)""",
		(memory_id, user_id, content[:1000], '{"source":"explicit_user_statement"}'),
	)
	conn.commit()
	return {"id": memory_id, "content": content[:1000], "memory_type": "explicit"}


def search_memories(
	conn: sqlite3.Connection,
	query: str,
	user_id: str = "local",
	limit: int = 8,
) -> list[dict[str, Any]]:
	"""检索与当前问题相关的长期记忆。

	Args:
		conn: 业务数据库连接。
		query: 用户问题。
		user_id: 本地用户命名空间。
		limit: 最大返回数量。

	Returns:
		相关记忆；关键词未命中时返回最近记忆。
	"""
	init_assistant_tables(conn)
	terms = [term for term in query.replace("，", " ").replace("。", " ").split() if len(term) >= 2][:5]
	if terms:
		conditions = " OR ".join("content LIKE ?" for _ in terms)
		params: list[Any] = [user_id, *[f"%{term}%" for term in terms], limit]
		rows = conn.execute(
			f"""SELECT id, memory_type, content, confidence, updated_at
			FROM assistant_memories
			WHERE user_id = ? AND status = 'active' AND ({conditions})
			ORDER BY updated_at DESC LIMIT ?""",
			params,
		).fetchall()
		if rows:
			return [dict(row) for row in rows]
	rows = conn.execute(
		"""SELECT id, memory_type, content, confidence, updated_at
		FROM assistant_memories
		WHERE user_id = ? AND status = 'active'
		ORDER BY updated_at DESC LIMIT ?""",
		(user_id, limit),
	).fetchall()
	return [dict(row) for row in rows]


def list_memories(conn: sqlite3.Connection, user_id: str = "local") -> list[dict[str, Any]]:
	"""列出当前用户的长期记忆。

	Args:
		conn: 业务数据库连接。
		user_id: 本地用户命名空间。

	Returns:
		有效记忆列表。
	"""
	init_assistant_tables(conn)
	rows = conn.execute(
		"""SELECT id, memory_type, content, confidence, created_at, updated_at
		FROM assistant_memories WHERE user_id = ? AND status = 'active'
		ORDER BY updated_at DESC""",
		(user_id,),
	).fetchall()
	return [dict(row) for row in rows]


def forget_memory(conn: sqlite3.Connection, memory_id: str, user_id: str = "local") -> bool:
	"""软删除一条长期记忆。

	Args:
		conn: 业务数据库连接。
		memory_id: 记忆 ID。
		user_id: 本地用户命名空间。

	Returns:
		是否找到并删除。
	"""
	cursor = conn.execute(
		"""UPDATE assistant_memories SET status = 'deleted', updated_at = CURRENT_TIMESTAMP
		WHERE id = ? AND user_id = ? AND status = 'active'""",
		(memory_id, user_id),
	)
	conn.commit()
	return cursor.rowcount > 0


BAD_CASE_CATEGORIES = {
	"intent_mismatch",
	"routing_error",
	"incorrect_answer",
	"context_missed",
	"unsafe_action",
	"other",
}


def create_bad_case(
	conn: sqlite3.Connection,
	conversation_id: str,
	assistant_message_id: str,
	category: str,
	expected_intent: str = "",
	feedback: str = "",
) -> dict[str, Any]:
	"""保存一条带完整推理上下文的助手 Bad Case。

	Args:
		conn: 业务数据库连接。
		conversation_id: Bad Case 所属会话。
		assistant_message_id: 被评价的助手消息 ID。
		category: 用户选择的问题分类。
		expected_intent: 用户认为正确的目标或意图。
		feedback: 用户补充的正确做法。

	Returns:
		可供前端确认展示的 Bad Case 摘要。

	Raises:
		ValueError: 分类无效、消息不存在或消息不属于指定会话。
	"""
	init_assistant_tables(conn)
	if category not in BAD_CASE_CATEGORIES:
		raise ValueError("无效的 Bad Case 分类")
	row = conn.execute(
		"""SELECT id, content, metadata_json FROM assistant_messages
		WHERE id = ? AND conversation_id = ? AND role = 'assistant'""",
		(assistant_message_id, conversation_id),
	).fetchone()
	if row is None:
		raise ValueError("被评价的助手消息不存在")
	user_row = conn.execute(
		"""SELECT id, content FROM assistant_messages
		WHERE conversation_id = ? AND role = 'user' AND rowid < (
			SELECT rowid FROM assistant_messages WHERE id = ?
		) ORDER BY rowid DESC LIMIT 1""",
		(conversation_id, assistant_message_id),
	).fetchone()
	try:
		metadata = json.loads(row["metadata_json"] or "{}")
	except (json.JSONDecodeError, TypeError):
		metadata = {}
	intent = metadata.get("intent") if isinstance(metadata.get("intent"), dict) else {}
	routing = {
		"result_summary": metadata.get("result_summary", {}),
		"capabilities": metadata.get("capabilities", []),
		"tool_trace": metadata.get("tool_trace", []),
		"execution_plan": metadata.get("execution_plan", []),
		"execution_events": metadata.get("execution_events", []),
		"plan_evaluation": metadata.get("plan_evaluation"),
		"replan_count": metadata.get("replan_count", 0),
	}
	bad_case_id = str(uuid.uuid4())
	conn.execute(
		"""INSERT INTO assistant_bad_cases (
			id, conversation_id, assistant_message_id, user_message_id, category,
			expected_intent, feedback, user_message, assistant_answer, intent_json, routing_json
		) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(assistant_message_id) DO UPDATE SET
			category = excluded.category,
			expected_intent = excluded.expected_intent,
			feedback = excluded.feedback,
			intent_json = excluded.intent_json,
			routing_json = excluded.routing_json,
			status = 'open',
			updated_at = CURRENT_TIMESTAMP""",
		(
			bad_case_id,
			conversation_id,
			assistant_message_id,
			user_row["id"] if user_row else None,
			category,
			expected_intent.strip()[:120],
			feedback.strip()[:2000],
			str(user_row["content"] if user_row else ""),
			str(row["content"]),
			json.dumps(intent, ensure_ascii=False),
			json.dumps(routing, ensure_ascii=False),
		),
	)
	conn.commit()
	saved = conn.execute(
		"""SELECT id, assistant_message_id, category, expected_intent, feedback, status, created_at, updated_at
		FROM assistant_bad_cases WHERE assistant_message_id = ?""",
		(assistant_message_id,),
	).fetchone()
	return dict(saved)


def list_bad_cases(conn: sqlite3.Connection, status: str = "open", limit: int = 200) -> list[dict[str, Any]]:
	"""列出用于意图识别和路由优化的 Bad Case 数据集。

	Args:
		conn: 业务数据库连接。
		status: open、resolved 或 all。
		limit: 最大返回条数。

	Returns:
		包含输入、回答及路由快照的 Bad Case 列表。
	"""
	init_assistant_tables(conn)
	where = "" if status == "all" else "WHERE status = ?"
	params: tuple[Any, ...] = (max(1, min(limit, 1000)),) if status == "all" else (status, max(1, min(limit, 1000)))
	rows = conn.execute(
		f"""SELECT id, conversation_id, assistant_message_id, user_message_id, category,
		expected_intent, feedback, user_message, assistant_answer, intent_json, routing_json,
		status, created_at, updated_at FROM assistant_bad_cases {where}
		ORDER BY created_at DESC LIMIT ?""",
		params,
	).fetchall()
	result = []
	for row in rows:
		item = dict(row)
		for field in ("intent_json", "routing_json"):
			try:
				item[field.removesuffix("_json")] = json.loads(item.pop(field))
			except (json.JSONDecodeError, TypeError):
				item[field.removesuffix("_json")] = {}
		result.append(item)
	return result


def rate_assistant_message(
	conn: sqlite3.Connection,
	conversation_id: str,
	assistant_message_id: str,
	rating: str,
) -> dict[str, Any]:
	"""持久化用户对单条助手回答的好坏手势。

	Args:
		conn: 业务数据库连接。
		conversation_id: 消息所属会话。
		assistant_message_id: 被评价的助手消息。
		rating: helpful 或 unhelpful。

	Returns:
		保存后的评价。

	Raises:
		ValueError: 评价无效或消息不存在。
	"""
	init_assistant_tables(conn)
	if rating not in {"helpful", "unhelpful"}:
		raise ValueError("无效的回答评价")
	message = conn.execute(
		"SELECT id FROM assistant_messages WHERE id = ? AND conversation_id = ? AND role = 'assistant'",
		(assistant_message_id, conversation_id),
	).fetchone()
	if message is None:
		raise ValueError("被评价的助手消息不存在")
	conn.execute(
		"""INSERT INTO assistant_message_ratings (assistant_message_id, conversation_id, rating)
		VALUES (?, ?, ?)
		ON CONFLICT(assistant_message_id) DO UPDATE SET
			rating = excluded.rating, updated_at = CURRENT_TIMESTAMP""",
		(assistant_message_id, conversation_id, rating),
	)
	conn.commit()
	return {"message_id": assistant_message_id, "rating": rating}
