"""最终回答阶段的上下文压缩工具。"""

from __future__ import annotations

import json
from typing import Any


MAX_JOB_ROWS = 50
MAX_HISTORY_MESSAGES = 8
MAX_WEB_SOURCES = 12
MAX_RESUME_CHARS = 12_000
MAX_TOOL_RESULT_CHARS = 8_000
# 超长业务字段按字段级截断，避免 SELECT * 兜底时完整 JD 撑爆回答上下文。
LONG_FIELD_CHARS = 500
LONG_FIELDS = {"jd", "score_reason", "greeting", "detail", "attributes_json"}
MAX_ANY_VALUE_CHARS = 2_000


def _truncate_rows(rows: list[Any]) -> list[Any]:
	"""对行内长文本字段做字符级截断，行数截断由调用方负责。"""
	truncated = []
	for row in rows:
		if not isinstance(row, dict):
			truncated.append(row)
			continue
		item: dict[str, Any] = {}
		for key, value in row.items():
			if isinstance(value, str) and len(value) > MAX_ANY_VALUE_CHARS:
				item[key] = value[:MAX_ANY_VALUE_CHARS] + "…[已截断]"
			elif key in LONG_FIELDS and isinstance(value, str) and len(value) > LONG_FIELD_CHARS:
				item[key] = value[:LONG_FIELD_CHARS] + "…[已截断]"
			else:
				item[key] = value
		truncated.append(item)
	return truncated


def compact_job_context(raw_context: str) -> str:
	"""压缩岗位查询结果，保留总数、口径和有限明细。"""
	try:
		payload = json.loads(raw_context)
	except (TypeError, ValueError):
		return raw_context[:MAX_TOOL_RESULT_CHARS]
	if not isinstance(payload, dict):
		return raw_context[:MAX_TOOL_RESULT_CHARS]
	rows = payload.get("rows", [])
	if not isinstance(rows, list):
		rows = []
	total_count = int(payload.get("total_count", payload.get("row_count", len(rows))) or 0)
	returned_count = int(payload.get("returned_count", len(rows)) or 0)
	visible_rows = rows[:MAX_JOB_ROWS]
	has_more = bool(payload.get("has_more", total_count > returned_count)) or returned_count > len(visible_rows)
	compact = {
		"mode": payload.get("mode", "detail"),
		"row_count": total_count,
		"total_count": total_count,
		"returned_count": returned_count,
		"visible_row_count": len(visible_rows),
		"has_more": has_more,
		"rows": _truncate_rows(visible_rows),
	}
	if payload.get("metric"):
		compact["metric"] = payload["metric"]
	if has_more:
		compact["truncated"] = True
	return json.dumps(compact, ensure_ascii=False, default=str)


def compact_answer_inputs(
	*,
	history: list[dict[str, Any]],
	resume: str,
	memories: list[dict[str, Any]],
	web_results: list[dict[str, Any]],
	tool_results: list[dict[str, Any]],
	history_prebounded: bool = False,
) -> dict[str, Any]:
	"""生成最终回答所需的最小充分上下文，避免重复提交大块工具原文。"""
	history_items = history if history_prebounded else history[-MAX_HISTORY_MESSAGES:]
	compact_history = [
		{
			"role": str(item.get("role", "")),
			"content": str(item.get("content", ""))[:2_000],
			**({"message_type": item["message_type"]} if item.get("message_type") else {}),
		}
		for item in history_items
	]
	compact_memories = [
		{**item, "content": str(item.get("content", ""))[:1_000]}
		for item in memories[-10:]
	]
	compact_web = [
		{
			"title": item.get("title", ""),
			"url": item.get("url", ""),
			"excerpt": str(item.get("excerpt", ""))[:600],
		}
		for item in web_results[:MAX_WEB_SOURCES]
	]
	compact_tools: list[dict[str, Any]] = []
	for item in tool_results:
		if item.get("name") == "job_database":
			continue
		result = item.get("result")
		serialized = json.dumps(result, ensure_ascii=False, default=str)
		if len(serialized) > MAX_TOOL_RESULT_CHARS:
			result = {"truncated": True, "preview": serialized[:MAX_TOOL_RESULT_CHARS]}
		compact_tools.append({"name": item.get("name"), "status": item.get("status", "completed"), "result": result})
	return {
		"history": compact_history,
		"resume": resume[:MAX_RESUME_CHARS],
		"memories": compact_memories,
		"web_results": compact_web,
		"tool_results": compact_tools,
	}


__all__ = ["compact_answer_inputs", "compact_job_context"]
