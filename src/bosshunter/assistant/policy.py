"""意图约束和安全策略的确定性处理。"""

from typing import Any, cast, get_args

from bosshunter.assistant.models import IntentAction, IntentConstraints, IntentResult, IntentStep


ACTION_TO_CAPABILITY: dict[IntentAction, str] = {
	"job_search": "job_collect",
	"job_score": "job_score",
	"job_query": "job_query",
	"company_research": "company_analysis",
	"greeting_generate": "greeting_generate",
	"greeting_send": "greeting_send",
	"reply_draft": "reply_draft",
	"reply_send": "reply_send",
	"reply_monitor": "reply_monitor",
	"pipeline_run": "pipeline",
}

_ACTIONS = set(get_args(IntentAction))
_POLICY_CONSTRAINT_FIELDS = {"forbidden_actions", "requires_confirmation", "do_not_send"}


def extract_explicit_constraints(question: str) -> IntentConstraints:
	"""从用户原话提取必须由程序强制执行的否定约束。

	Args:
		question: 当前用户原始消息。

	Returns:
		不依赖模型判断的安全约束。
	"""
	normalized = question.replace(" ", "").lower()
	forbidden: list[IntentAction] = []
	negative_send_terms = ("不要发送", "别发送", "先不发送", "禁止发送", "只生成", "不要投递", "别投递")
	if any(term in normalized for term in negative_send_terms):
		forbidden.extend(["greeting_send", "reply_send"])
	if any(term in normalized for term in ("不要回复hr", "别回复hr", "不要回复人事")):
		forbidden.append("reply_send")
	if any(term in normalized for term in ("不要打招呼", "别打招呼")):
		forbidden.append("greeting_send")
	if not forbidden:
		return {}
	return {
		"forbidden_actions": list(dict.fromkeys(forbidden)),
		"do_not_send": True,
	}


def apply_intent_policy(intent: IntentResult, question: str = "") -> IntentResult:
	"""合并显式约束并禁用冲突步骤。

	Args:
		intent: 模型或降级规则生成的结构化意图。
		question: 当前用户原话，用于确定性提取否定表达。

	Returns:
		不会执行被用户禁止动作的新意图对象。
	"""
	constraints: dict[str, Any] = dict(intent.get("constraints", {}))
	explicit = extract_explicit_constraints(question)
	forbidden = [item for item in constraints.get("forbidden_actions", []) if item in _ACTIONS]
	forbidden.extend(explicit.get("forbidden_actions", []))
	if constraints.get("do_not_send"):
		forbidden.extend(["greeting_send", "reply_send"])
	constraints.update(explicit)
	constraints["forbidden_actions"] = list(dict.fromkeys(forbidden))

	steps: list[IntentStep] = []
	for index, step in enumerate(intent.get("steps", []), start=1):
		action = step.get("action")
		if action not in _ACTIONS:
			continue
		arguments = step.get("arguments") if isinstance(step.get("arguments"), dict) else {}
		dependencies = step.get("depends_on") if isinstance(step.get("depends_on"), list) else []
		steps.append(
			{
				"id": str(step.get("id") or f"step_{index}"),
				"action": cast(IntentAction, action),
				"arguments": arguments,
				"depends_on": [str(item) for item in dependencies],
				"enabled": bool(step.get("enabled", True) and action not in forbidden),
				"reason": str(step.get("reason", "")),
			}
		)
	return {**intent, "constraints": constraints, "steps": steps}  # type: ignore[typeddict-item]


def enabled_steps(intent: IntentResult) -> list[IntentStep]:
	"""返回通过用户约束过滤后的可执行步骤。"""
	forbidden = set(intent.get("constraints", {}).get("forbidden_actions", []))
	return [
		step
		for step in intent.get("steps", [])
		if step["enabled"] and step["action"] not in forbidden
	]


def tool_constraints(intent: IntentResult) -> dict[str, Any]:
	"""仅返回可作为工具参数的非策略约束。"""
	return {
		key: value
		for key, value in intent.get("constraints", {}).items()
		if key not in _POLICY_CONSTRAINT_FIELDS
	}


__all__ = [
	"ACTION_TO_CAPABILITY",
	"apply_intent_policy",
	"enabled_steps",
	"extract_explicit_constraints",
	"tool_constraints",
]
