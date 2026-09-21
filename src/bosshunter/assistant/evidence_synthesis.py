"""统一证据结果的校验、引用生成和回答上下文构造。"""

from __future__ import annotations

import json
from typing import Any

from bosshunter.assistant.models import EvidenceResult, EvidenceTask


def evaluate_evidence_coverage(
	tasks: list[EvidenceTask],
	results: list[EvidenceResult],
) -> dict[str, Any]:
	"""检查所有必需证据任务是否得到可用结果。"""
	by_id = {result["task_id"]: result for result in results}
	missing = [
		task["id"]
		for task in tasks
		if task["required"] and (
			task["id"] not in by_id or by_id[task["id"]]["status"] not in {"completed", "partial"}
		)
	]
	return {"complete": not missing, "missing_task_ids": missing}


def build_evidence_context(results: list[EvidenceResult]) -> str:
	"""构造有来源边界的有界回答上下文。"""
	payload = [
		{
			"task_id": result["task_id"],
			"executor": result["executor"],
			"status": result["status"],
			"summary": result["summary"],
			"evidence": result["evidence"],
			"missing_evidence": result["missing_evidence"],
			"data": result["data"],
		}
		for result in results
	]
	return json.dumps(payload, ensure_ascii=False, default=str)


def evidence_citations(results: list[EvidenceResult]) -> list[dict[str, str]]:
	"""从标准证据中生成去重引用。"""
	citations: list[dict[str, str]] = []
	seen: set[tuple[str, str]] = set()
	for result in results:
		for item in result["evidence"]:
			key = (item["source_type"], item["source_id"])
			if key in seen:
				continue
			seen.add(key)
			citation = {"source": item["source_type"], "label": item["source_label"]}
			if item["source_type"] == "public_current" and item["source_id"].startswith(("http://", "https://")):
				citation["url"] = item["source_id"]
			citations.append(citation)
	return citations


__all__ = ["build_evidence_context", "evaluate_evidence_coverage", "evidence_citations"]
