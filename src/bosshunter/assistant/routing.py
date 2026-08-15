"""基于完整性、置信度、风险和复杂度的决策路由。"""

from bosshunter.assistant.models import IntentResult, RouteName


_HIGH_CONFIDENCE_ACTIONS = {
	"job_search",
	"job_score",
	"greeting_generate",
	"greeting_send",
	"reply_send",
	"reply_monitor",
	"pipeline_run",
	"complex_task",
}


def decide_route(intent: IntentResult, complex_goal: bool) -> RouteName:
	"""选择理解层之后的固定流程分支。

	Args:
		intent: 已完成歧义检查的意图。
		complex_goal: 是否需要 Planner。

	Returns:
		LangGraph 中预先注册的固定路由名称。
	"""
	if intent["missing_fields"] or intent["confidence"] < 0.6:
		return "clarification"
	# 只有会启动任务或产生副作用的动作需要二次确认意图。
	# 只读查询、公司研究和回复草稿可在中等置信度下继续，避免咨询问题被反复拦截。
	if intent["confidence"] < 0.85 and intent["intent_type"] in _HIGH_CONFIDENCE_ACTIONS:
		return "clarification"
	if complex_goal:
		return "planner"
	if intent["intent_type"] == "general_question":
		return "direct"
	return "capability"


__all__ = ["decide_route"]
