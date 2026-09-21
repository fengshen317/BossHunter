"""简历、岗位和用户偏好的轻量私有资料执行器。"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any


def execute_private_documents(
	question: str,
	resume: str,
	dependency_results: dict[str, Any],
	config: dict[str, Any],
	model_call: Callable[[str, dict[str, Any]], str],
) -> dict[str, Any]:
	"""结合简历与前序候选提取匹配事实和入围公司。

	Args:
		question: 用户问题。
		resume: 当前用户简历。
		dependency_results: 上游问数结果。
		config: AI 配置。
		model_call: 文本模型调用函数。

	Returns:
		有界的匹配摘要；不修改岗位或用户资料。
	"""
	if not resume.strip():
		return {"content": "", "analysis": "未读取到简历", "selected_companies": [], "selected_jobs": []}
	dependency_text = json.dumps(dependency_results, ensure_ascii=False, default=str)[:10_000]
	prompt = f"""你是 BossHunter 私有资料匹配执行器。只根据简历和候选岗位提取匹配事实，不得联网，不得编造经历。

用户问题：{question}

简历：
{resume[:12_000]}

前序候选岗位：
{dependency_text or "未提供"}

请输出简洁 JSON：
{{"analysis":"匹配结论","selected_jobs":[{{"id":"岗位ID","title":"岗位","company":"公司","reason":"原因"}}],"selected_companies":["公司"]}}
最多保留 5 个岗位和 5 家公司。资料不足时返回空数组，不要猜测。"""
	answer = model_call(prompt, config) or ""
	parsed = _extract_json(answer)
	return {
		"content": resume[:12_000],
		"analysis": str(parsed.get("analysis") or answer)[:4_000],
		"selected_jobs": _limited_dicts(parsed.get("selected_jobs"), 5),
		"selected_companies": _limited_strings(parsed.get("selected_companies"), 5),
	}


def _extract_json(text: str) -> dict[str, Any]:
	"""从模型文本中提取单个 JSON 对象。"""
	start, end = text.find("{"), text.rfind("}")
	if start < 0 or end <= start:
		return {}
	try:
		value = json.loads(text[start : end + 1])
	except (TypeError, ValueError):
		return {}
	return value if isinstance(value, dict) else {}


def _limited_dicts(value: Any, limit: int) -> list[dict[str, str]]:
	"""保留少量可序列化字典字段。"""
	if not isinstance(value, list):
		return []
	return [
		{str(key): str(item_value)[:500] for key, item_value in item.items()}
		for item in value[:limit]
		if isinstance(item, dict)
	]


def _limited_strings(value: Any, limit: int) -> list[str]:
	"""保留少量去重非空字符串。"""
	if not isinstance(value, list):
		return []
	return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))[:limit]


__all__ = ["execute_private_documents"]
