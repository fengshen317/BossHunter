"""Agent 能力注册表和能力匹配逻辑。"""

from typing import Any

from bosshunter.assistant.models import CapabilityMatch, CapabilitySpec, IntentResult, RiskLevel
from bosshunter.assistant.policy import ACTION_TO_CAPABILITY, apply_intent_policy, enabled_steps, tool_constraints


CAPABILITY_REGISTRY: dict[str, CapabilitySpec] = {
	"job_collect": {
		"name": "job_collect",
		"description": "按关键词和城市采集岗位",
		"tool_name": "collect_jobs",
		"intents": ["job_search"],
		"required_fields": ["keywords"],
		"risk_level": "medium",
		"read_only": False,
		"requires_approval": True,
	},
	"job_score": {
		"name": "job_score",
		"description": "对候选岗位执行 AI 评分",
		"tool_name": "score_jobs",
		"intents": ["job_score"],
		"required_fields": [],
		"risk_level": "medium",
		"read_only": False,
		"requires_approval": True,
	},
	"job_query": {
		"name": "job_query",
		"description": "查询本地岗位数据库",
		"tool_name": "job_database",
		"intents": ["job_query"],
		"required_fields": [],
		"risk_level": "low",
		"read_only": True,
		"requires_approval": False,
	},
	"company_analysis": {
		"name": "company_analysis",
		"description": "搜索并汇总公司公开信息",
		"tool_name": "company_research",
		"intents": ["company_research"],
		"required_fields": ["company"],
		"risk_level": "low",
		"read_only": True,
		"requires_approval": False,
	},
	"greeting_generate": {
		"name": "greeting_generate",
		"description": "根据岗位和简历生成招呼语",
		"tool_name": "generate_greetings",
		"intents": ["greeting_generate"],
		"required_fields": [],
		"risk_level": "medium",
		"read_only": False,
		"requires_approval": True,
	},
	"greeting_send": {
		"name": "greeting_send",
		"description": "发送用户选中的招呼语",
		"tool_name": "send_greetings",
		"intents": ["greeting_send"],
		"required_fields": ["job_ids"],
		"risk_level": "high",
		"read_only": False,
		"requires_approval": True,
	},
	"reply_draft": {
		"name": "reply_draft",
		"description": "结合岗位上下文生成 HR 回复草稿",
		"tool_name": None,
		"intents": ["reply_draft"],
		"required_fields": [],
		"risk_level": "medium",
		"read_only": False,
		"requires_approval": True,
	},
	"reply_send": {
		"name": "reply_send",
		"description": "发送用户确认后的 HR 回复",
		"tool_name": "send_hr_reply",
		"intents": ["reply_send"],
		"required_fields": ["history_id", "message"],
		"risk_level": "high",
		"read_only": False,
		"requires_approval": True,
	},
	"reply_monitor": {
		"name": "reply_monitor",
		"description": "启动 HR 回复监测",
		"tool_name": "monitor_replies",
		"intents": ["reply_monitor"],
		"required_fields": [],
		"risk_level": "medium",
		"read_only": False,
		"requires_approval": True,
	},
	"pipeline": {
		"name": "pipeline",
		"description": "执行完整求职流水线",
		"tool_name": "run_pipeline",
		"intents": ["pipeline_run"],
		"required_fields": [],
		"risk_level": "high",
		"read_only": False,
		"requires_approval": True,
	},
}


def capabilities_for_intent(intent: IntentResult) -> list[CapabilitySpec]:
	"""查找支持当前意图的能力。

	Args:
		intent: 结构化意图。

	Returns:
		注册表中匹配的能力元数据。
	"""
	intent = apply_intent_policy(intent, intent["goal"])
	structured_steps = enabled_steps(intent)
	if intent.get("steps"):
		selected = list(
			dict.fromkeys(ACTION_TO_CAPABILITY[step["action"]] for step in structured_steps)
		)
		return [CAPABILITY_REGISTRY[name] for name in selected if name in CAPABILITY_REGISTRY]
	if intent["intent_type"] != "complex_task":
		return [spec for spec in CAPABILITY_REGISTRY.values() if intent["intent_type"] in spec["intents"]]
	goal = intent["goal"].replace(" ", "")
	selected: list[str] = []
	# 旧会话可能没有结构化步骤，保留关键词规则仅用于兼容，不作为新请求的主路径。
	for terms, capability in (
		(("采集", "搜索", "查找"), "job_collect"),
		(("评分", "打分"), "job_score"),
		(("生成招呼语", "写招呼语"), "greeting_generate"),
		(("发送招呼语", "投递"), "greeting_send"),
		(("监测回复", "监听回复"), "reply_monitor"),
	):
		if any(term in goal for term in terms):
			selected.append(capability)
	return [CAPABILITY_REGISTRY[name] for name in selected]


def complete_missing_fields(intent: IntentResult) -> IntentResult:
	"""根据能力元数据补齐必填槽位清单并提升最低风险等级。

	Args:
		intent: 结构化意图。

	Returns:
		补齐缺失字段且风险不低于能力定义的意图。
	"""
	# 模型可能臆造 candidate_name、job_id 等并非能力契约要求的槽位。
	# 缺失字段必须由能力注册表统一裁决，否则普通咨询会永久停在澄清节点。
	result: IntentResult = {**apply_intent_policy(intent, intent["goal"]), "missing_fields": []}
	entities = result["entities"]
	for spec in capabilities_for_intent(result):
		for field in spec["required_fields"]:
			if not entities.get(field) and field not in result["missing_fields"]:
				result["missing_fields"].append(field)
		risk_order: dict[RiskLevel, int] = {"low": 0, "medium": 1, "high": 2}
		if risk_order[spec["risk_level"]] > risk_order[result["risk_level"]]:
			result["risk_level"] = spec["risk_level"]
	return result


def match_capabilities(intent: IntentResult) -> list[CapabilityMatch]:
	"""把意图实体映射为可执行能力和工具参数。

	Args:
		intent: 已通过完整性检查的意图。

	Returns:
		供执行层消费的能力匹配结果。
	"""
	intent = apply_intent_policy(intent, intent["goal"])
	base_arguments: dict[str, Any] = {**intent["entities"], **tool_constraints(intent)}
	steps = enabled_steps(intent)
	results: list[CapabilityMatch] = []
	for spec in capabilities_for_intent(intent):
		arguments = dict(base_arguments)
		step = next((item for item in steps if ACTION_TO_CAPABILITY[item["action"]] == spec["name"]), None)
		if step:
			arguments.update(step["arguments"])
		results.append(
			{
			"capability": spec["name"],
			"tool_name": spec["tool_name"],
			"arguments": arguments,
			"risk_level": spec["risk_level"],
			"read_only": spec["read_only"],
			"requires_approval": spec["requires_approval"],
			}
		)
	return results


__all__ = ["CAPABILITY_REGISTRY", "capabilities_for_intent", "complete_missing_fields", "match_capabilities"]
