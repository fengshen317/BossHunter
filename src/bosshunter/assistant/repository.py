"""AI 助手的会话、消息与长期记忆持久化。"""

import json
import hashlib
import re
import sqlite3
import uuid
from typing import Any

from bosshunter.assistant.context_budget import estimate_text_tokens


_MEMORY_KEY_PATTERN = re.compile(r"[^a-z0-9_.-]+")


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
			memory_key TEXT,
			category TEXT NOT NULL DEFAULT 'profile',
			content TEXT NOT NULL,
			value_json TEXT NOT NULL DEFAULT '{}',
			source_type TEXT NOT NULL DEFAULT 'explicit',
			source_conversation_id TEXT,
			source_message_id TEXT,
			source_excerpt TEXT NOT NULL DEFAULT '',
			supersedes_id TEXT,
			metadata_json TEXT NOT NULL DEFAULT '{}',
			confidence REAL NOT NULL DEFAULT 1.0,
			status TEXT NOT NULL DEFAULT 'active',
			created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
			updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
		);

		CREATE TABLE IF NOT EXISTS assistant_conversation_contexts (
			conversation_id TEXT PRIMARY KEY,
			summary_text TEXT NOT NULL DEFAULT '',
			summary_token_count INTEGER NOT NULL DEFAULT 0,
			summarized_through_message_id TEXT,
			pending_digest_json TEXT NOT NULL DEFAULT '[]',
			pending_intent_json TEXT NOT NULL DEFAULT '{}',
			active_goal_json TEXT NOT NULL DEFAULT '{}',
			updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
			FOREIGN KEY (conversation_id) REFERENCES assistant_conversations(id)
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
		ON assistant_messages(conversation_id, created_at, id);
		CREATE INDEX IF NOT EXISTS idx_assistant_memories_user
		ON assistant_memories(user_id, status, updated_at);
		CREATE INDEX IF NOT EXISTS idx_assistant_bad_cases_status
		ON assistant_bad_cases(status, created_at);
	""")
	_migrate_sqlite_assistant_tables(conn)
	conn.commit()


def _migrate_sqlite_assistant_tables(conn: Any) -> None:
	"""为已有 SQLite 数据库补充上下文和版本化记忆字段。"""
	if not isinstance(conn, sqlite3.Connection):
		return
	columns = {row[1] for row in conn.execute("PRAGMA table_info(assistant_memories)").fetchall()}
	definitions = {
		"memory_key": "TEXT",
		"category": "TEXT NOT NULL DEFAULT 'profile'",
		"value_json": "TEXT NOT NULL DEFAULT '{}'",
		"source_type": "TEXT NOT NULL DEFAULT 'explicit'",
		"source_conversation_id": "TEXT",
		"source_message_id": "TEXT",
		"source_excerpt": "TEXT NOT NULL DEFAULT ''",
		"supersedes_id": "TEXT",
	}
	for name, definition in definitions.items():
		if name not in columns:
			conn.execute(f"ALTER TABLE assistant_memories ADD COLUMN {name} {definition}")
	conn.execute(
		"UPDATE assistant_memories SET memory_key = 'legacy.' || id WHERE memory_key IS NULL OR memory_key = ''"
	)
	conn.execute(
		"CREATE UNIQUE INDEX IF NOT EXISTS idx_assistant_memories_active_key "
		"ON assistant_memories(user_id, memory_key) WHERE status = 'active'"
	)


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


def ensure_conversation_row(
	conn: sqlite3.Connection,
	conversation_id: str,
	title: str = "恢复的会话",
) -> None:
	"""确保会话行存在（幂等），不存在时补建空会话。

	前端可能携带历史会话 ID（数据库迁移 / 清库前浏览器 localStorage 的
	残留），直接写入消息会触发 assistant_messages 的外键约束。这里在写
	消息前补建会话行，让旧 ID 自愈为空会话而不是报错。

	Args:
		conn: 业务数据库连接。
		conversation_id: 会话 ID。
		title: 补建时使用的会话标题。
	"""
	init_assistant_tables(conn)
	exists = conn.execute(
		"SELECT 1 FROM assistant_conversations WHERE id = ?",
		(conversation_id,),
	).fetchone()
	if exists is None:
		conn.execute(
			"INSERT INTO assistant_conversations (id, title) VALUES (?, ?)",
			(conversation_id, title[:60] or "恢复的会话"),
		)
		conn.commit()


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
			 WHERE m.conversation_id = c.id ORDER BY m.created_at DESC, m.id DESC LIMIT 1) AS preview
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
		(
			message_id,
			conversation_id,
			role,
			content,
			message_type,
			json.dumps(metadata or {}, ensure_ascii=False, default=str),
		),
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
		ORDER BY created_at, id""",
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


def list_message_page(
	conn: Any,
	conversation_id: str,
	*,
	before: str | None = None,
	limit: int = 40,
) -> dict[str, Any]:
	"""按游标读取一页消息，返回顺序始终为从旧到新。"""
	init_assistant_tables(conn)
	page_limit = max(1, min(int(limit), 100))
	params: list[Any] = [conversation_id]
	boundary = ""
	if before:
		cursor = conn.execute(
			"SELECT created_at, id FROM assistant_messages WHERE conversation_id = ? AND id = ?",
			(conversation_id, before),
		).fetchone()
		if cursor is None:
			raise ValueError("消息游标无效或不属于当前会话")
		boundary = " AND (created_at < ? OR (created_at = ? AND id < ?))"
		params.extend([cursor["created_at"], cursor["created_at"], cursor["id"]])
	params.append(page_limit + 1)
	rows = conn.execute(
		f"""SELECT id, conversation_id, role, content, message_type, metadata_json, created_at
		FROM assistant_messages WHERE conversation_id = ?{boundary}
		ORDER BY created_at DESC, id DESC LIMIT ?""",
		params,
	).fetchall()
	has_more = len(rows) > page_limit
	page_rows = list(rows[:page_limit])
	page_rows.reverse()
	items: list[dict[str, Any]] = []
	for row in page_rows:
		item = dict(row)
		try:
			item["metadata"] = json.loads(item.pop("metadata_json"))
		except (json.JSONDecodeError, TypeError):
			item["metadata"] = {}
		items.append(item)
	return {
		"items": items,
		"next_cursor": items[0]["id"] if has_more and items else None,
		"has_more": has_more,
	}


def get_conversation_context(conn: Any, conversation_id: str) -> dict[str, Any]:
	"""读取会话滚动摘要和结构化进行中状态。"""
	init_assistant_tables(conn)
	row = conn.execute(
		"""SELECT conversation_id, summary_text, summary_token_count,
		summarized_through_message_id, pending_digest_json, pending_intent_json,
		active_goal_json, updated_at FROM assistant_conversation_contexts
		WHERE conversation_id = ?""",
		(conversation_id,),
	).fetchone()
	if row is None:
		return {
			"conversation_id": conversation_id,
			"summary_text": "",
			"summary_token_count": 0,
			"summarized_through_message_id": None,
			"pending_digest": [],
			"pending_intent": {},
			"active_goal": {},
		}
	item = dict(row)
	for field in ("pending_digest_json", "pending_intent_json", "active_goal_json"):
		try:
			item[field.removesuffix("_json")] = json.loads(item.pop(field) or "{}")
		except (json.JSONDecodeError, TypeError):
			item[field.removesuffix("_json")] = [] if field == "pending_digest_json" else {}
	return item


def save_conversation_summary(
	conn: Any,
	conversation_id: str,
	*,
	summary_text: str,
	summary_token_count: int,
	summarized_through_message_id: str,
) -> None:
	"""原子更新会话滚动摘要游标，避免重复压缩同一批消息。"""
	init_assistant_tables(conn)
	conn.execute(
		"""INSERT INTO assistant_conversation_contexts (
		conversation_id, summary_text, summary_token_count, summarized_through_message_id
		) VALUES (?, ?, ?, ?)
		ON CONFLICT(conversation_id) DO UPDATE SET
			summary_text = excluded.summary_text,
			summary_token_count = excluded.summary_token_count,
			summarized_through_message_id = excluded.summarized_through_message_id,
			updated_at = CURRENT_TIMESTAMP""",
		(conversation_id, summary_text, summary_token_count, summarized_through_message_id),
	)
	conn.commit()


def save_pending_intent(conn: Any, conversation_id: str, intent: dict[str, Any] | None) -> None:
	"""保存或清空等待用户补充的结构化意图。"""
	init_assistant_tables(conn)
	conn.execute(
		"""INSERT INTO assistant_conversation_contexts (conversation_id, pending_intent_json)
		VALUES (?, ?)
		ON CONFLICT(conversation_id) DO UPDATE SET
			pending_intent_json = excluded.pending_intent_json,
			updated_at = CURRENT_TIMESTAMP""",
		(conversation_id, json.dumps(intent or {}, ensure_ascii=False, default=str)),
	)
	conn.commit()


def remember(conn: Any, content: str, user_id: str = "local") -> dict[str, Any]:
	"""保存用户明确要求记住的信息。

	Args:
		conn: 业务数据库连接。
		content: 记忆正文。
		user_id: 本地用户命名空间。

	Returns:
		新记忆记录。
	"""
	init_assistant_tables(conn)
	normalized = " ".join(content.split())[:1000]
	digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]
	return upsert_memory(
		conn,
		memory_key=f"explicit.{digest}",
		category="profile",
		content=normalized,
		value=normalized,
		source_type="explicit",
		confidence=1.0,
		user_id=user_id,
	)


def upsert_memory(
	conn: Any,
	*,
	memory_key: str,
	category: str,
	content: str,
	value: Any,
	source_type: str,
	confidence: float,
	user_id: str = "local",
	source_conversation_id: str | None = None,
	source_message_id: str | None = None,
	source_excerpt: str = "",
) -> dict[str, Any]:
	"""以不可变版本写入记忆，并自动替换同键旧值。"""
	init_assistant_tables(conn)
	key = _normalize_memory_key(memory_key)
	category_value = category if category in {"profile", "preference", "constraint", "workflow", "ongoing_goal"} else "profile"
	content_value = " ".join(str(content).split())[:1000]
	value_json = json.dumps(value, ensure_ascii=False, default=str)
	current = conn.execute(
		"""SELECT id, value_json FROM assistant_memories
		WHERE user_id = ? AND memory_key = ? AND status = 'active' LIMIT 1""",
		(user_id, key),
	).fetchone()
	if current and str(current["value_json"]) == value_json:
		conn.execute(
			"""UPDATE assistant_memories SET content = ?, confidence = ?, source_type = ?,
			source_conversation_id = ?, source_message_id = ?, source_excerpt = ?,
			updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
			(
				content_value,
				max(0.0, min(float(confidence), 1.0)),
				source_type,
				source_conversation_id,
				source_message_id,
				source_excerpt[:500],
				current["id"],
			),
		)
		conn.commit()
		return {"id": current["id"], "memory_key": key, "content": content_value, "operation": "unchanged"}
	if current:
		conn.execute(
			"UPDATE assistant_memories SET status = 'superseded', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
			(current["id"],),
		)
	memory_id = str(uuid.uuid4())
	conn.execute(
		"""INSERT INTO assistant_memories (
		id, user_id, memory_type, memory_key, category, content, value_json,
		source_type, source_conversation_id, source_message_id, source_excerpt,
		supersedes_id, metadata_json, confidence, status
		) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')""",
		(
			memory_id,
			user_id,
			source_type,
			key,
			category_value,
			content_value,
			value_json,
			source_type,
			source_conversation_id,
			source_message_id,
			source_excerpt[:500],
			current["id"] if current else None,
			json.dumps({"source": source_type}, ensure_ascii=False),
			max(0.0, min(float(confidence), 1.0)),
		),
	)
	conn.commit()
	return {
		"id": memory_id,
		"memory_key": key,
		"category": category_value,
		"content": content_value,
		"memory_type": source_type,
		"operation": "superseded" if current else "created",
		"supersedes_id": current["id"] if current else None,
	}


def search_memories(
	conn: Any,
	query: str,
	user_id: str = "local",
	limit: int = 8,
	max_tokens: int = 2_048,
) -> list[dict[str, Any]]:
	"""检索与当前问题相关的长期记忆。

	Args:
		conn: 业务数据库连接。
		query: 用户问题。
		user_id: 本地用户命名空间。
		limit: 最大返回数量。

	Returns:
		按中文相关性排序的记忆；无相关结果时返回空列表。
	"""
	init_assistant_tables(conn)
	rows = conn.execute(
		"""SELECT id, memory_type, memory_key, category, content, value_json,
		source_type, source_conversation_id, source_message_id, source_excerpt,
		confidence, status, created_at, updated_at
		FROM assistant_memories
		WHERE user_id = ? AND status = 'active'
		ORDER BY updated_at DESC LIMIT 500""",
		(user_id,),
	).fetchall()
	query_normalized = _normalize_search_text(query)
	query_bigrams = _bigrams(query_normalized)
	scored: list[tuple[float, dict[str, Any]]] = []
	for row in rows:
		item = dict(row)
		candidate = _normalize_search_text(f"{item.get('memory_key', '')} {item.get('content', '')}")
		candidate_bigrams = _bigrams(candidate)
		overlap = len(query_bigrams & candidate_bigrams)
		score = overlap / max(1, len(query_bigrams))
		if item.get("category") in {"constraint", "workflow"}:
			score += 0.2
		if str(item.get("memory_key", "")) in query_normalized:
			score += 1.0
		if overlap or item.get("category") in {"constraint", "workflow"}:
			scored.append((score, item))
	scored.sort(key=lambda pair: (pair[0], str(pair[1].get("updated_at", ""))), reverse=True)
	selected: list[dict[str, Any]] = []
	used_tokens = 0
	for _, item in scored:
		item_tokens = max(1, estimate_text_tokens(str(item.get("content", ""))))
		if selected and used_tokens + item_tokens > max_tokens:
			continue
		selected.append(item)
		used_tokens += item_tokens
		if len(selected) >= max(1, limit):
			break
	return selected


def _normalize_memory_key(value: str) -> str:
	"""生成可用于唯一索引的稳定记忆键。"""
	normalized = _MEMORY_KEY_PATTERN.sub("_", str(value).strip().lower()).strip("._-")
	return normalized[:120] or f"memory.{uuid.uuid4().hex[:20]}"


def _normalize_search_text(value: str) -> str:
	"""移除中文检索中的无意义分隔符。"""
	return re.sub(r"[^a-z0-9\u3400-\u9fff]+", "", str(value).lower())


def _bigrams(value: str) -> set[str]:
	"""生成无需外部分词器的字符二元组。"""
	if len(value) < 2:
		return {value} if value else set()
	return {value[index : index + 2] for index in range(len(value) - 1)}


def list_memories(conn: Any, user_id: str = "local", status: str = "active") -> list[dict[str, Any]]:
	"""列出当前用户的长期记忆。

	Args:
		conn: 业务数据库连接。
		user_id: 本地用户命名空间。

	Returns:
		有效记忆列表。
	"""
	init_assistant_tables(conn)
	where = "status = 'active'" if status == "active" else "status IN ('active', 'superseded', 'deleted')"
	rows = conn.execute(
		f"""SELECT id, memory_type, memory_key, category, content, value_json,
		source_type, source_conversation_id, source_message_id, source_excerpt,
		supersedes_id, confidence, status, created_at, updated_at
		FROM assistant_memories WHERE user_id = ? AND {where}
		ORDER BY updated_at DESC""",
		(user_id,),
	).fetchall()
	return [dict(row) for row in rows]


def edit_memory(
	conn: Any,
	memory_id: str,
	content: str,
	*,
	user_id: str = "local",
) -> dict[str, Any] | None:
	"""以新版本修改一条有效记忆，保留原记录用于审计。"""
	init_assistant_tables(conn)
	row = conn.execute(
		"""SELECT memory_key, category FROM assistant_memories
		WHERE id = ? AND user_id = ? AND status = 'active'""",
		(memory_id, user_id),
	).fetchone()
	cleaned = " ".join(content.split())[:1000]
	if row is None or not cleaned:
		return None
	return upsert_memory(
		conn,
		memory_key=str(row["memory_key"]),
		category=str(row["category"]),
		content=cleaned,
		value=cleaned,
		source_type="explicit_edit",
		confidence=1.0,
		user_id=user_id,
		source_excerpt=cleaned,
	)


def restore_memory(conn: Any, memory_id: str, user_id: str = "local") -> dict[str, Any] | None:
	"""把历史版本恢复成新的有效版本，不篡改旧审计记录。"""
	init_assistant_tables(conn)
	row = conn.execute(
		"""SELECT memory_key, category, content, value_json FROM assistant_memories
		WHERE id = ? AND user_id = ?""",
		(memory_id, user_id),
	).fetchone()
	if row is None:
		return None
	try:
		value = json.loads(row["value_json"])
	except (json.JSONDecodeError, TypeError):
		value = row["content"]
	return upsert_memory(
		conn,
		memory_key=str(row["memory_key"]),
		category=str(row["category"]),
		content=str(row["content"]),
		value=value,
		source_type="explicit_restore",
		confidence=1.0,
		user_id=user_id,
		source_excerpt=str(row["content"]),
	)


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
		WHERE conversation_id = ? AND role = 'user' AND created_at <= (
			SELECT created_at FROM assistant_messages WHERE id = ?
		) ORDER BY created_at DESC, id DESC LIMIT 1""",
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
			json.dumps(intent, ensure_ascii=False, default=str),
			json.dumps(routing, ensure_ascii=False, default=str),
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
