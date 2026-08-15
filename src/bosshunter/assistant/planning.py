"""复杂任务规划和 DAG 构造。"""

from bosshunter.assistant.models import CapabilityMatch, IntentResult, PlanEvaluation, PlanStep


_PIPELINE_STEPS: list[PlanStep] = [
	{
		"id": "collect",
		"label": "采集符合条件的岗位",
		"capability": "job_collect",
		"depends_on": [],
		"status": "pending",
		"requires_approval": False,
	},
	{
		"id": "score",
		"label": "对采集结果进行 AI 评分",
		"capability": "job_score",
		"depends_on": ["collect"],
		"status": "pending",
		"requires_approval": False,
	},
	{
		"id": "greeting",
		"label": "为通过评分的岗位生成招呼语",
		"capability": "greeting_generate",
		"depends_on": ["score"],
		"status": "pending",
		"requires_approval": False,
	},
	{
		"id": "approve",
		"label": "等待用户逐项确认",
		"capability": "human_approval",
		"depends_on": ["greeting"],
		"status": "approval_required",
		"requires_approval": True,
	},
	{
		"id": "send",
		"label": "低频发送已确认的招呼语",
		"capability": "greeting_send",
		"depends_on": ["approve"],
		"status": "pending",
		"requires_approval": True,
	},
]


def build_plan(intent: IntentResult, matches: list[CapabilityMatch]) -> list[PlanStep]:
	"""根据复杂目标和能力匹配结果生成可恢复的执行计划。

	Args:
		intent: 结构化意图。
		matches: 能力匹配结果。

	Returns:
		按依赖顺序排列的 DAG 步骤。
	"""
	if intent["intent_type"] == "pipeline_run":
		return [dict(step) for step in _PIPELINE_STEPS]  # type: ignore[misc]
	steps: list[PlanStep] = []
	previous_id: str | None = None
	for index, match in enumerate(matches, start=1):
		step_id = f"step_{index}"
		steps.append(
			{
				"id": step_id,
				"label": match["capability"],
				"capability": match["capability"],
				"depends_on": [previous_id] if previous_id else [],
				"status": "approval_required" if match["requires_approval"] else "pending",
				"requires_approval": match["requires_approval"],
			}
		)
		previous_id = step_id
	return steps


def evaluate_plan(plan: list[PlanStep], available_capabilities: set[str]) -> PlanEvaluation:
	"""检查计划步骤、能力和依赖关系是否可执行。

	Args:
		plan: 待评估的 DAG。
		available_capabilities: 当前能力注册表中的能力名称。

	Returns:
		包含有效性、原因和失败步骤的评估结果。
	"""
	if not plan:
		return {"valid": False, "reason": "计划为空，无法执行。", "failed_step_id": None}
	step_ids = {step["id"] for step in plan}
	completed: set[str] = set()
	for step in plan:
		if step["capability"] not in available_capabilities | {"human_approval"}:
			return {
				"valid": False,
				"reason": f"能力 {step['capability']} 未注册。",
				"failed_step_id": step["id"],
			}
		if any(dependency not in step_ids for dependency in step["depends_on"]):
			return {"valid": False, "reason": "计划包含不存在的依赖步骤。", "failed_step_id": step["id"]}
		if any(dependency not in completed for dependency in step["depends_on"]):
			return {"valid": False, "reason": "计划依赖顺序无效或存在循环。", "failed_step_id": step["id"]}
		completed.add(step["id"])
	return {"valid": True, "reason": "计划结构和能力检查通过。", "failed_step_id": None}


def rebuild_plan(
	intent: IntentResult,
	matches: list[CapabilityMatch],
	previous_plan: list[PlanStep],
) -> list[PlanStep]:
	"""保留已完成步骤，并根据最新能力重新构建剩余计划。

	Args:
		intent: 原始结构化目标。
		matches: 最新能力匹配结果。
		previous_plan: 上一版执行计划。

	Returns:
		包含已完成状态的新计划版本。
	"""
	completed = {step["capability"] for step in previous_plan if step["status"] == "completed"}
	new_plan = build_plan(intent, matches)
	for step in new_plan:
		if step["capability"] in completed:
			step["status"] = "completed"
	return new_plan


__all__ = ["build_plan", "evaluate_plan", "rebuild_plan"]
