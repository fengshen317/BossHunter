"""把跨领域问题转换成证据任务和可调度计划。"""

from __future__ import annotations

from bosshunter.assistant.models import EvidenceDomain, EvidenceRoute, EvidenceTask, PlanStep


_TASK_CONFIG: dict[EvidenceDomain, tuple[str, str, str, float]] = {
	"structured_data": ("query_business_data", "查询结构化业务数据", "job_query", 30.0),
	"private_document": ("retrieve_private_context", "检索简历、岗位与用户偏好", "resume_read", 10.0),
	"public_current": ("research_public_information", "研究入围对象的公开信息", "web_search", 60.0),
}


def build_evidence_tasks(question: str, route: EvidenceRoute) -> list[EvidenceTask]:
	"""根据证据域建立有序、可审计的依赖任务。

	Args:
		question: 用户原始问题。
		route: 证据域路由。

	Returns:
		遵循问数、私有匹配、联网研究顺序的证据任务。
	"""
	tasks: list[EvidenceTask] = []
	previous_id: str | None = None
	for domain in ("structured_data", "private_document", "public_current"):
		if domain not in route["domains"]:
			continue
		task_id, label, _, timeout = _TASK_CONFIG[domain]
		tasks.append(
			{
				"id": task_id,
				"question": question,
				"evidence_type": domain,
				"depends_on": [previous_id] if previous_id else [],
				"required": True,
				"max_results": 5 if domain == "public_current" else 50,
				"timeout_seconds": timeout,
				"status": "pending",
			}
		)
		previous_id = task_id
	return tasks


def evidence_tasks_to_plan(tasks: list[EvidenceTask]) -> list[PlanStep]:
	"""把证据任务转换成现有前端和 DAG 调度器兼容的计划。"""
	plan: list[PlanStep] = []
	for task in tasks:
		_, label, capability, _ = _TASK_CONFIG[task["evidence_type"]]
		plan.append(
			{
				"id": task["id"],
				"label": label,
				"capability": capability,
				"depends_on": list(task["depends_on"]),
				"status": task["status"],
				"requires_approval": False,
				"arguments": {
					"question": task["question"],
					"evidence_type": task["evidence_type"],
					"max_results": task["max_results"],
				},
				"timeout_seconds": task["timeout_seconds"],
			}
		)
	return plan


__all__ = ["build_evidence_tasks", "evidence_tasks_to_plan"]
