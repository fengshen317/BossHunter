"""长期记忆候选校验、敏感信息过滤与版本化写入。"""

from __future__ import annotations

import re
from typing import Any

_ALLOWED_CATEGORIES = {"profile", "preference", "constraint", "workflow", "ongoing_goal"}
_ALLOWED_OPERATIONS = {"upsert"}
_MEMORY_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{2,119}$")
_SENSITIVE_PATTERNS = (
	re.compile(r"(?:api[_ -]?key|token|密码|口令|密钥)\s*[:：=]", re.IGNORECASE),
	re.compile(r"\b1[3-9]\d{9}\b"),
	re.compile(r"\b\d{17}[\dXx]\b"),
	re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b"),
	re.compile(r"\b\d{15,19}\b"),
)


def normalize_memory_operations(
	payload: Any,
	user_text: str,
	*,
	min_confidence: float = 0.85,
) -> list[dict[str, Any]]:
	"""只保留能回指用户原文且通过隐私策略的记忆候选。"""
	if not isinstance(payload, list):
		return []
	result: list[dict[str, Any]] = []
	for raw in payload[:10]:
		if not isinstance(raw, dict) or raw.get("operation") not in _ALLOWED_OPERATIONS:
			continue
		key = str(raw.get("memory_key", "")).strip().lower()
		category = str(raw.get("category", "")).strip().lower()
		content = " ".join(str(raw.get("content", "")).split())[:1000]
		excerpt = str(raw.get("source_excerpt", "")).strip()[:500]
		try:
			confidence = float(raw.get("confidence", 0.0))
		except (TypeError, ValueError):
			continue
		if (
			confidence < min_confidence
			or category not in _ALLOWED_CATEGORIES
			or not _MEMORY_KEY_PATTERN.fullmatch(key)
			or not content
			or not excerpt
			or excerpt not in user_text
			or _contains_sensitive_data(excerpt)
			or _contains_sensitive_data(content)
		):
			continue
		result.append(
			{
				"operation": "upsert",
				"memory_key": key,
				"category": category,
				"content": content,
				"value": raw.get("value", content),
				"source_excerpt": excerpt,
				"confidence": min(confidence, 1.0),
			}
		)
	return result


def persist_memory_operations(
	conn: Any,
	operations: list[dict[str, Any]],
	*,
	conversation_id: str,
	message_id: str,
	user_id: str = "local",
) -> list[dict[str, Any]]:
	"""在同一业务库中应用已校验的记忆写入操作。"""
	# 延迟导入可避免开发服务器热重载时 repository 尚未初始化完成，
	# 导致模块级导入拿不到刚定义的版本化写入函数。
	from bosshunter.assistant.repository import upsert_memory

	updates: list[dict[str, Any]] = []
	for operation in operations:
		updates.append(
			upsert_memory(
				conn,
				memory_key=str(operation["memory_key"]),
				category=str(operation["category"]),
				content=str(operation["content"]),
				value=operation.get("value"),
				source_type="automatic",
				confidence=float(operation["confidence"]),
				user_id=user_id,
				source_conversation_id=conversation_id,
				source_message_id=message_id,
				source_excerpt=str(operation["source_excerpt"]),
			)
		)
	return updates


def _contains_sensitive_data(value: str) -> bool:
	"""使用确定性规则拦截不应自动持久化的敏感原文。"""
	return any(pattern.search(value) for pattern in _SENSITIVE_PATTERNS)


__all__ = ["normalize_memory_operations", "persist_memory_operations"]
