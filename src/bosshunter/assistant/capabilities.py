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

# 兼容期保留 intents 字段供旧路由使用，通用 Agent 依赖以下可检索元数据。
_CAPABILITY_METADATA: dict[str, dict[str, Any]] = {
	"job_collect": {"tags": ["jobs"], "side_effect": "local_persistent"},
	"job_score": {"tags": ["jobs"], "side_effect": "local_persistent"},
	"job_query": {"tags": ["jobs", "database"], "side_effect": "none"},
	"company_analysis": {"tags": ["company", "research", "web"], "side_effect": "none"},
	"greeting_generate": {"tags": ["jobs", "send"], "side_effect": "local_persistent"},
	"greeting_send": {"tags": ["jobs", "send"], "side_effect": "external"},
	"reply_draft": {"tags": ["send"], "side_effect": "local_reversible"},
	"reply_send": {"tags": ["send"], "side_effect": "external"},
	"reply_monitor": {"tags": ["monitor"], "side_effect": "external"},
	"pipeline": {"tags": ["jobs", "send", "monitor"], "side_effect": "external"},
}
for _capability_name, _spec in CAPABILITY_REGISTRY.items():
	_metadata = _CAPABILITY_METADATA[_capability_name]
	_spec.update(
		{
			"when_to_use": [_spec["description"]],
			"when_not_to_use": [],
			"input_schema": {"type": "object", "properties": {}},
			"output_schema": {"type": "object"},
			"side_effect": _metadata["side_effect"],
			"timeout_seconds": 30,
			"cost_level": "low" if _spec["read_only"] else "medium",
			"tags": _metadata["tags"],
		}
	)

CAPABILITY_REGISTRY.update(
	{
		"resume_read": {
			"name": "resume_read", "description": "读取当前用户简历，为岗位匹配和求职建议提供依据",
			"tool_name": "resume_read", "intents": [], "required_fields": [], "risk_level": "low",
			"read_only": True, "requires_approval": False, "when_to_use": ["简历", "匹配"],
			"when_not_to_use": ["用户未要求结合个人经历"],
			"input_schema": {"type": "object", "properties": {}}, "output_schema": {"type": "object"},
			"side_effect": "none", "timeout_seconds": 5, "cost_level": "low", "tags": ["resume"],
		},
		"web_search": {
			"name": "web_search", "description": "搜索公开网页以补充时效性事实和研究资料",
			"tool_name": "web_search", "intents": [], "required_fields": [], "risk_level": "low",
			"read_only": True, "requires_approval": False, "when_to_use": ["公开信息", "搜索", "研究"],
			"when_not_to_use": ["本地数据已经足够"],
			"input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
			"output_schema": {"type": "object"}, "side_effect": "none", "timeout_seconds": 20,
			"cost_level": "medium", "tags": ["research", "web"],
		},
	}
)


def _arguments_for_capability(intent: IntentResult, capability_name: str) -> dict[str, Any]:
	"""合并实体与对应步骤参数，作为能力的统一槽位来源。

	模型有时会把已抽取的实体仅放在 steps.arguments。完整性检查和
	工具调用必须读取同一份合并结果，避免已提供的信息被误判为缺失。

	Args:
		intent: 已通过策略过滤的结构化意图。
		capability_name: 能力注册表中的能力名称。

	Returns:
		可供该能力使用的标准化参数。
	"""
	arguments: dict[str, Any] = {}
	for step in enabled_steps(intent):
		if ACTION_TO_CAPABILITY[step["action"]] == capability_name:
			arguments.update(step["arguments"])
			break
	# entities 是实体的唯一权威来源；步骤参数仅用于兼容旧会话和承载非实体参数。
	# 最后覆盖可防止模型在 steps.arguments 中用冲突值改写已确认实体。
	arguments.update(tool_constraints(intent))
	arguments.update(intent["entities"])
	return arguments


def entity_contracts_by_intent() -> dict[str, list[str]]:
	"""从能力注册表生成各意图的必填实体契约。

	Returns:
		意图名称到必填实体字段列表的映射。
	"""
	contracts: dict[str, list[str]] = {}
	for spec in CAPABILITY_REGISTRY.values():
		for intent_type in spec["intents"]:
			contracts[intent_type] = list(spec["required_fields"])
	return contracts


def canonicalize_capability_entities(intent: IntentResult) -> IntentResult:
	"""将能力必填实体统一收敛到 ``entities`` 并同步执行步骤。

	新请求以 entities 为唯一权威来源；若旧模型仅把必填实体写入步骤参数，
	则先兼容性回收到 entities。随后把权威值同步到对应步骤，避免规划与执行
	看到不同参数。

	Args:
		intent: 已归一化的结构化意图。

	Returns:
		实体和步骤参数一致的新意图。
	"""
	entities = dict(intent["entities"])
	steps = [{**step, "arguments": dict(step["arguments"])} for step in intent.get("steps", [])]
	for spec in capabilities_for_intent(intent):
		matching_steps = [
			step for step in steps if ACTION_TO_CAPABILITY[step["action"]] == spec["name"]
		]
		for field in spec["required_fields"]:
			if not entities.get(field):
				for step in matching_steps:
					if step["arguments"].get(field):
						entities[field] = step["arguments"][field]
						break
			if entities.get(field):
				for step in matching_steps:
					step["arguments"][field] = entities[field]
	return {**intent, "entities": entities, "steps": steps}


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
	result = canonicalize_capability_entities(apply_intent_policy(intent, intent["goal"]))
	result = {**result, "missing_fields": []}
	for spec in capabilities_for_intent(result):
		arguments = _arguments_for_capability(result, spec["name"])
		for field in spec["required_fields"]:
			if not arguments.get(field) and field not in result["missing_fields"]:
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
	intent = canonicalize_capability_entities(apply_intent_policy(intent, intent["goal"]))
	results: list[CapabilityMatch] = []
	for spec in capabilities_for_intent(intent):
		arguments = _arguments_for_capability(intent, spec["name"])
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


__all__ = [
	"CAPABILITY_REGISTRY",
	"canonicalize_capability_entities",
	"capabilities_for_intent",
	"complete_missing_fields",
	"entity_contracts_by_intent",
	"match_capabilities",
]
