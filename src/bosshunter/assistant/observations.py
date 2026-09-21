"""Agent 工具结果的统一 Observation 适配。"""

from typing import Any

from bosshunter.assistant.models import ReactObservation


def build_observation(
	tool_name: str,
	arguments: dict[str, Any],
	status: str,
	result: Any,
) -> ReactObservation:
	"""把任意工具结果标准化为有摘要、事实和错误的观察。"""
	failed = status == "failed"
	facts: dict[str, Any] = {}
	if isinstance(result, dict):
		for key in ("count", "row_count", "company", "title"):
			if key in result:
				facts[key] = result[key]
	error = str(result.get("error")) if failed and isinstance(result, dict) and result.get("error") else None
	summary = error or _result_summary(result)
	return {
		"tool_name": tool_name,
		"arguments": arguments,
		"status": "failed" if failed else "completed",
		"result": result,
		"summary": summary,
		"facts": facts,
		"error": error,
	}


def _result_summary(result: Any) -> str:
	"""生成稳定、紧凑的工具结果摘要。"""
	if isinstance(result, dict):
		count = result.get("row_count", result.get("count"))
		if count is not None:
			return f"获得 {count} 条结果"
		return f"获得包含 {len(result)} 个字段的结果"
	if isinstance(result, list):
		return f"获得 {len(result)} 条结果"
	return str(result)[:200]


__all__ = ["build_observation"]
