"""专业执行器原始结果到统一证据协议的轻量适配。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from bosshunter.assistant.models import EvidenceDomain, EvidenceItem, EvidenceResult, EvidenceStatus


_TOOL_DOMAINS: dict[str, EvidenceDomain] = {
	"job_database": "structured_data",
	"resume_read": "private_document",
	"company_research": "public_current",
	"web_search": "public_current",
	"deep_research": "public_current",
}


def evidence_domain_for_tool(tool_name: str) -> EvidenceDomain | None:
	"""返回只读工具所属的唯一证据域。"""
	return _TOOL_DOMAINS.get(tool_name)


def build_evidence_result(
	task_id: str,
	executor: str,
	source_type: EvidenceDomain,
	raw_result: Any,
	*,
	status: EvidenceStatus = "completed",
	summary: str = "",
	error: str | None = None,
) -> EvidenceResult:
	"""把执行器结果包装成可追溯的统一协议。

	Args:
		task_id: 证据任务 ID。
		executor: 执行器名称。
		source_type: 主证据域。
		raw_result: 原始工具结果。
		status: 执行状态。
		summary: 面向综合器的摘要。
		error: 可选错误。

	Returns:
		不展开大结果的标准证据结果。
	"""
	items = _evidence_items(raw_result, source_type)
	return {
		"task_id": task_id,
		"executor": executor,
		"status": status,
		"evidence": items,
		"summary": summary or _summary(raw_result, items),
		"missing_evidence": [] if status == "completed" else ["执行器未获得完整证据"],
		"error": error,
		"data_ref": None,
		"data": _compact_data(raw_result, source_type),
	}


def _evidence_items(raw_result: Any, source_type: EvidenceDomain) -> list[EvidenceItem]:
	"""提取少量结构化事实，原始大结果仍由现有制品机制管理。"""
	now = datetime.now().astimezone().isoformat(timespec="seconds")
	if source_type == "structured_data" and isinstance(raw_result, str):
		try:
			raw_result = json.loads(raw_result)
		except (TypeError, ValueError):
			raw_result = {}
	if source_type == "structured_data" and isinstance(raw_result, dict):
		row_count = int(raw_result.get("row_count", len(raw_result.get("rows", []))) or 0)
		return [_item("查询返回记录数", row_count, source_type, "business_database", "BossHunter 业务数据库", now, 1.0)]
	if source_type == "private_document" and isinstance(raw_result, dict):
		content = str(raw_result.get("content", "")).strip()
		return [_item("已读取相关私有资料", bool(content), source_type, "resume", "当前简历", now, 0.95)]
	if source_type == "public_current":
		sources = _public_sources(raw_result)
		items = [_item("公开来源数量", len(sources), source_type, "web", "公开网页", now, 0.8)]
		for index, source in enumerate(sources[:8], start=1):
			items.append(
				_item(
					str(source.get("title") or source.get("url") or f"公开来源 {index}"),
					str(source.get("excerpt", ""))[:600],
					source_type,
					str(source.get("url") or f"web-{index}"),
					str(source.get("title") or "公开网页"),
					now,
					0.8,
				)
			)
		return items
	return []


def _compact_data(raw_result: Any, source_type: EvidenceDomain) -> Any:
	"""为最终综合保留有界数据，避免重新拼装无界原始工具结果。"""
	if source_type == "structured_data":
		if isinstance(raw_result, str):
			try:
				raw_result = json.loads(raw_result)
			except (TypeError, ValueError):
				return raw_result[:8_000]
		if isinstance(raw_result, dict):
			return {**raw_result, "rows": list(raw_result.get("rows", []))[:50]}
	if source_type == "private_document" and isinstance(raw_result, dict):
		return {**raw_result, "content": str(raw_result.get("content", ""))[:12_000]}
	if source_type == "public_current":
		if isinstance(raw_result, dict):
			return {**raw_result, "sources": _public_sources(raw_result)[:12]}
		return list(raw_result or [])[:12]
	return raw_result


def _public_sources(raw_result: Any) -> list[dict[str, Any]]:
	"""从基础搜索、深度研究或公司背调结果中归一化网页来源。"""
	if isinstance(raw_result, list):
		if raw_result and all(isinstance(item, dict) and "company" in item for item in raw_result):
			return [source for item in raw_result for source in _company_sources(item)]
		return [item for item in raw_result if isinstance(item, dict)]
	if not isinstance(raw_result, dict):
		return []
	if isinstance(raw_result.get("sources"), list):
		return [item for item in raw_result["sources"] if isinstance(item, dict)]
	return _company_sources(raw_result)


def _company_sources(result: dict[str, Any]) -> list[dict[str, Any]]:
	"""展开公司研究主题中的来源。"""
	return [
		source
		for topic in result.get("topics", [])
		if isinstance(topic, dict)
		for source in topic.get("results", [])
		if isinstance(source, dict)
	]


def _item(
	claim: str,
	value: Any,
	source_type: EvidenceDomain,
	source_id: str,
	source_label: str,
	observed_at: str,
	confidence: float,
) -> EvidenceItem:
	"""创建单条证据。"""
	return {
		"claim": claim,
		"value": value,
		"source_type": source_type,
		"source_id": source_id,
		"source_label": source_label,
		"source_location": None,
		"observed_at": observed_at,
		"effective_at": None,
		"confidence": confidence,
	}


def _summary(raw_result: Any, items: list[EvidenceItem]) -> str:
	"""生成有界摘要。"""
	if items:
		return "；".join(f"{item['claim']}：{item['value']}" for item in items)
	return "未获得可结构化的证据" if raw_result is None else "执行器已返回结果"


__all__ = ["build_evidence_result", "evidence_domain_for_tool"]
