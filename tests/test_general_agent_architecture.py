"""通用 Agent 首个只读切片的架构回归测试。"""

from typing import Any

from bosshunter.assistant.capabilities import CAPABILITY_REGISTRY
from bosshunter.assistant.capability_retriever import retrieve_capabilities
from bosshunter.assistant.goal_understanding import build_goal_state
from bosshunter.assistant.models import IntentResult
from bosshunter.assistant.observations import build_observation


def _intent(**overrides: Any) -> IntentResult:
	"""创建兼容期意图，以验证它不再限制只读能力可见性。"""
	result: IntentResult = {
		"goal": "分析岗位与个人经历的匹配度",
		"intent_type": "general_question",
		"entities": {},
		"constraints": {},
		"missing_fields": [],
		"confidence": 0.9,
		"risk_level": "low",
		"reason": "测试",
	}
	result.update(overrides)
	return result


def test_general_goal_retrieves_multiple_readonly_capabilities_without_intent_gate() -> None:
	"""通用问答应根据目标召回岗位和简历，不受 general_question 标签限制。"""
	goal = build_goal_state("结合我的简历分析当前 Agent 岗位匹配度", _intent())

	matches = retrieve_capabilities(goal, CAPABILITY_REGISTRY, web_enabled=False)

	assert {item["capability"] for item in matches} >= {"job_query", "resume_read"}
	assert all(item["read_only"] and not item["requires_approval"] for item in matches)


def test_negative_send_constraint_filters_external_capabilities() -> None:
	"""模型或召回评分都不能绕过用户明确的禁止发送策略。"""
	intent = _intent(
		goal="分析岗位并准备建议，不要发送",
		constraints={"forbidden_actions": ["greeting_send", "reply_send"], "do_not_send": True},
	)
	goal = build_goal_state("分析岗位并准备建议，不要发送", intent)

	matches = retrieve_capabilities(goal, CAPABILITY_REGISTRY, web_enabled=True)

	assert all(CAPABILITY_REGISTRY[item["capability"]]["side_effect"] != "external" for item in matches)


def test_observation_has_summary_facts_and_error_contract() -> None:
	"""工具结果必须标准化，避免把无界原始结果作为唯一上下文。"""
	observation = build_observation("job_database", {}, "completed", {"row_count": 3, "rows": []})

	assert observation["summary"] == "获得 3 条结果"
	assert observation["facts"] == {"row_count": 3}
	assert observation["error"] is None
