"""将兼容期意图结果转换为通用 Agent 目标。"""

from typing import Any

from bosshunter.assistant.models import GoalState, IntentResult


def build_goal_state(user_request: str, intent: IntentResult) -> GoalState:
	"""构建不依赖唯一意图的目标、成功标准和约束。

	Args:
		user_request: 用户本轮原始请求。
		intent: 兼容期理解器输出。

	Returns:
		供能力检索和 Agent 循环消费的通用目标状态。
	"""
	criteria = _success_criteria(user_request, intent)
	constraints = _constraint_texts(intent)
	job_ids = intent["entities"].get("job_ids", [])
	if isinstance(job_ids, str):
		job_ids = [job_ids]
	return {
		"user_request": user_request,
		"goal": intent["goal"] or user_request,
		"success_criteria": criteria,
		"constraints": constraints,
		"entities": dict(intent["entities"]),
		"assumptions": [],
		"unknowns": list(intent["missing_fields"]),
		"selected_job_ids": [str(item) for item in job_ids],
		"completion_status": "pending",
	}


def _success_criteria(user_request: str, intent: IntentResult) -> list[str]:
	"""从显式步骤和目标生成可验证的成功标准。"""
	criteria = [step.get("reason") or step["action"] for step in intent.get("steps", []) if step.get("enabled", True)]
	if criteria:
		return list(dict.fromkeys(str(item) for item in criteria))
	return [intent["goal"] or user_request]


def _constraint_texts(intent: IntentResult) -> list[str]:
	"""把程序策略转换为可读约束，供模型和审计共同使用。"""
	constraints: list[str] = []
	policy = intent.get("constraints", {})
	for action in policy.get("forbidden_actions", []):
		constraints.append(f"禁止执行：{action}")
	if policy.get("do_not_send"):
		constraints.append("禁止发送或投递")
	if policy.get("requires_confirmation"):
		constraints.append("任何有副作用的动作必须人工确认")
	return list(dict.fromkeys(constraints))


def goal_search_text(goal: GoalState) -> str:
	"""生成确定性能力召回使用的文本。"""
	parts: list[Any] = [goal["user_request"], goal["goal"], *goal["success_criteria"]]
	parts.extend(goal["entities"].keys())
	parts.extend(str(value) for value in goal["entities"].values())
	return " ".join(str(part) for part in parts if part).lower()


__all__ = ["build_goal_state", "goal_search_text"]
