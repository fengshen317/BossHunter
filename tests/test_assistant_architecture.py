"""分层 Agent 架构的纯函数回归测试。"""

from typing import Any

from bosshunter.assistant.capabilities import complete_missing_fields, match_capabilities
from bosshunter.assistant.models import IntentResult
from bosshunter.assistant.planning import build_plan, evaluate_plan, rebuild_plan
from bosshunter.assistant.react import MAX_REACT_ITERATIONS, can_continue, initial_react_action, select_model_action
from bosshunter.assistant.routing import decide_route
from bosshunter.assistant.understanding import extract_goal, fallback_intent, is_complex_goal, resolve_context


def _intent(**overrides: Any) -> IntentResult:
	"""创建字段完整的测试意图。

	Args:
		**overrides: 需要覆盖的字段。

	Returns:
		可供各层纯函数消费的意图。
	"""
	result: IntentResult = {
		"goal": "查询岗位",
		"intent_type": "job_query",
		"entities": {},
		"constraints": {},
		"missing_fields": [],
		"confidence": 0.9,
		"risk_level": "low",
		"reason": "测试",
	}
	result.update(overrides)
	return result


def test_context_resolver_keeps_only_recent_trusted_inputs() -> None:
	"""上下文节点应提取最新问题和页面状态，不调用外部能力。"""
	state = {
		"messages": [
			{"role": "assistant", "content": "你好"},
			{"role": "user", "content": "查询当前岗位"},
		],
		"job_id": "job-1",
		"web_enabled": True,
	}

	context = resolve_context(state)

	assert context["question"] == "查询当前岗位"
	assert context["current_job_id"] == "job-1"
	assert context["web_enabled"] is True


def test_short_reply_fills_company_requested_by_previous_clarification() -> None:
	"""用户直接回复公司名时，应延续上一轮公司背调目标而不是重复追问。"""
	pending = _intent(
		goal="帮我查下这家公司的情况",
		intent_type="company_research",
		entities={},
		missing_fields=["company"],
		confidence=0.86,
		steps=[
			{
				"id": "research",
				"action": "company_research",
				"arguments": {},
				"depends_on": [],
				"enabled": True,
				"reason": "调查目标公司",
			}
		],
	)
	context = resolve_context(
		{
			"messages": [
				{"role": "user", "content": "帮我查下这家公司的情况"},
				{
					"role": "assistant",
					"content": "为了准确完成这个目标，请补充：公司名称。",
					"message_type": "clarification",
				},
				{"role": "user", "content": "运筹帷幄物流集团"},
			],
			"intent": pending,
		}
	)

	def model_call(prompt: str, config: dict[str, Any], max_tokens: int) -> str:
		"""模拟模型仍然遗漏公司名称的结果。"""
		assert "上一轮待补全意图" in prompt
		return """{
"goal":"调查公司","mode":"execute","intent_type":"company_research","entities":{},"constraints":{},
"steps":[],"missing_fields":["company"],"confidence":0.82,"risk_level":"low","reason":"缺少公司"
}"""

	intent = extract_goal(context, {}, model_call)
	checked = complete_missing_fields(intent)
	matches = match_capabilities(checked)

	assert intent["goal"] == "帮我查下这家公司的情况"
	assert intent["entities"]["company"] == "运筹帷幄物流集团"
	assert checked["missing_fields"] == []
	assert matches[0]["arguments"]["company"] == "运筹帷幄物流集团"


def test_goal_extractor_validates_json_and_rejects_understated_risk() -> None:
	"""模型 JSON 应先归一化，再由能力元数据提升最低风险。"""
	context = resolve_context({"messages": [{"role": "user", "content": "发送招呼语"}]})

	def model_call(prompt: str, config: dict[str, Any], max_tokens: int) -> str:
		"""返回故意低估风险的合法模型结果。"""
		assert prompt
		assert max_tokens == 600
		return """```json
{"goal":"发送招呼语","intent_type":"greeting_send","entities":{"job_ids":["job-1"]},
"constraints":{},"missing_fields":[],"confidence":0.95,"risk_level":"low","reason":"明确请求"}
```"""

	intent = extract_goal(context, {}, model_call)
	checked = complete_missing_fields(intent)

	assert checked["intent_type"] == "greeting_send"
	assert checked["risk_level"] == "high"
	assert checked["missing_fields"] == []


def test_invalid_model_output_falls_back_to_conservative_rules() -> None:
	"""模型没有返回 JSON 时应使用规则降级，而不是让异常中断图。"""
	context = resolve_context({"messages": [{"role": "user", "content": "帮我给岗位评分"}]})

	def model_call(prompt: str, config: dict[str, Any], max_tokens: int) -> str:
		"""模拟不可解析的模型输出。"""
		return "这不是 JSON"

	intent = extract_goal(context, {}, model_call)

	assert intent["intent_type"] == "job_score"
	assert intent["risk_level"] == "medium"


def test_missing_capability_slots_route_to_clarification() -> None:
	"""岗位搜索缺少关键词时必须先澄清，不能进入工具执行。"""
	intent = fallback_intent("帮我搜索岗位")
	checked = complete_missing_fields(intent)

	assert checked["missing_fields"] == ["keywords"]
	assert decide_route(checked, complex_goal=False) == "clarification"


def test_medium_confidence_action_routes_to_second_clarification() -> None:
	"""中置信度动作应先补充信息，避免直接进入有副作用的能力。"""
	intent = _intent(intent_type="job_score", confidence=0.7, risk_level="medium")

	assert decide_route(intent, complex_goal=False) == "clarification"


def test_model_invented_slots_do_not_block_reply_draft() -> None:
	"""回复草稿不应被模型臆造的候选人和岗位槽位卡住。"""
	intent = _intent(
		intent_type="reply_draft",
		confidence=0.7,
		missing_fields=["candidate_name", "job_id"],
	)

	checked = complete_missing_fields(intent)

	assert checked["missing_fields"] == []
	assert decide_route(checked, complex_goal=False) == "capability"


def test_medium_confidence_readonly_query_does_not_require_clarification() -> None:
	"""只读问题在意图基本明确时应继续回答，而不是反复追问。"""
	intent = _intent(intent_type="job_query", confidence=0.7, risk_level="low")

	assert decide_route(intent, complex_goal=False) == "capability"


def test_capability_matcher_uses_registry_metadata() -> None:
	"""完整意图应映射为工具、参数和审批策略。"""
	intent = _intent(
		goal="搜索 Python 岗位",
		intent_type="job_search",
		entities={"keywords": ["Python"], "cities": ["北京"]},
		risk_level="medium",
	)

	matches = match_capabilities(intent)

	assert matches == [
		{
			"capability": "job_collect",
			"tool_name": "collect_jobs",
			"arguments": {"keywords": ["Python"], "cities": ["北京"]},
			"risk_level": "medium",
			"read_only": False,
			"requires_approval": True,
		}
	]


def test_pipeline_goal_builds_dependency_plan_with_approval_gate() -> None:
	"""长任务计划必须显式包含依赖关系和发送前人工审批。"""
	intent = _intent(goal="执行完整流程", intent_type="pipeline_run", risk_level="high")
	matches = match_capabilities(intent)

	plan = build_plan(intent, matches)

	assert is_complex_goal(intent) is True
	assert decide_route(intent, complex_goal=True) == "planner"
	assert [step["id"] for step in plan] == ["collect", "score", "greeting", "approve", "send"]
	assert plan[-1]["depends_on"] == ["approve"]
	assert plan[-1]["requires_approval"] is True


def test_complex_goal_plans_only_explicitly_requested_capabilities() -> None:
	"""复杂目标不应被扩张成包含发送动作的完整流水线。"""
	intent = _intent(
		goal="搜索 Python 岗位并评分",
		intent_type="complex_task",
		entities={"keywords": ["Python"]},
		risk_level="medium",
	)

	matches = match_capabilities(intent)
	plan = build_plan(intent, matches)

	assert [item["capability"] for item in matches] == ["job_collect", "job_score"]
	assert [step["capability"] for step in plan] == ["job_collect", "job_score"]
	assert all(step["capability"] != "greeting_send" for step in plan)


def test_complex_goal_prefers_model_steps_and_honors_disabled_actions() -> None:
	"""模型明确禁止发送时，能力匹配不能再通过整句关键词把发送能力带回来。"""
	intent = _intent(
		goal="搜索岗位并生成招呼语，不要发送",
		intent_type="complex_task",
		entities={"keywords": ["Python"]},
		steps=[
			{"action": "job_search", "enabled": True},
			{"action": "greeting_generate", "enabled": True},
			{"action": "greeting_send", "enabled": False},
		],
	)

	matches = match_capabilities(intent)

	assert [item["capability"] for item in matches] == ["job_collect", "greeting_generate"]


def test_explicit_negative_constraint_overrides_model_send_step() -> None:
	"""用户原话明确禁止发送时，模型即使启用发送步骤也必须被策略层覆盖。"""
	context = resolve_context(
		{"messages": [{"role": "user", "content": "搜索 Python 岗位并生成招呼语，但不要发送"}]}
	)

	def model_call(prompt: str, config: dict[str, Any], max_tokens: int) -> str:
		"""模拟遗漏否定约束的模型结果。"""
		return """{
"goal":"搜索 Python 岗位并生成招呼语，但不要发送","mode":"execute","intent_type":"complex_task",
"entities":{"keywords":["Python"]},"constraints":{},"missing_fields":[],"confidence":0.96,
"risk_level":"high","reason":"多步骤任务","steps":[
{"id":"search","action":"job_search","arguments":{},"depends_on":[],"enabled":true,"reason":"搜索"},
{"id":"draft","action":"greeting_generate","arguments":{},"depends_on":["search"],"enabled":true,"reason":"生成"},
{"id":"send","action":"greeting_send","arguments":{},"depends_on":["draft"],"enabled":true,"reason":"发送"}
]}"""

	intent = extract_goal(context, {}, model_call)
	matches = match_capabilities(intent)
	plan = build_plan(intent, matches)

	assert intent["constraints"]["forbidden_actions"] == ["greeting_send", "reply_send"]
	assert [step["id"] for step in intent["steps"] if step["enabled"]] == ["search", "draft"]
	assert [item["capability"] for item in matches] == ["job_collect", "greeting_generate"]
	assert [step["id"] for step in plan] == ["search", "draft"]
	assert plan[1]["depends_on"] == ["search"]


def test_step_arguments_are_isolated_from_policy_constraints() -> None:
	"""每个步骤使用自己的参数，禁止动作等策略字段不得泄漏给工具。"""
	intent = _intent(
		goal="搜索深圳 Python 岗位",
		intent_type="complex_task",
		constraints={"forbidden_actions": ["greeting_send"], "do_not_send": True},
		steps=[
			{
				"id": "search",
				"action": "job_search",
				"arguments": {"cities": ["深圳"]},
				"depends_on": [],
				"enabled": True,
				"reason": "搜索目标岗位",
			}
		],
		entities={"keywords": ["Python"]},
	)

	matches = match_capabilities(intent)

	assert matches[0]["arguments"] == {"keywords": ["Python"], "cities": ["深圳"]}
	assert "forbidden_actions" not in matches[0]["arguments"]


def test_answer_mode_routes_directly_even_when_model_uses_action_intent() -> None:
	"""咨询模式优先于动作标签，避免“怎么搜索岗位”被误当成执行请求。"""
	intent = _intent(intent_type="job_search", mode="answer")

	assert decide_route(intent, complex_goal=False) == "direct"


def test_react_only_selects_registered_readonly_tools() -> None:
	"""ReAct 不得选择发送或写库能力，即使模型尝试调用。"""
	intent = _intent(intent_type="job_query")
	matches = match_capabilities(intent)
	initial = initial_react_action(matches, [])
	model_action = select_model_action(
		[
			{"name": "send_greetings", "arguments": {"job_ids": ["job-1"]}},
			{"name": "job_database", "arguments": {}},
		],
		matches,
		[],
	)

	assert initial is not None
	assert initial["tool_name"] == "job_database"
	assert model_action is not None
	assert model_action["tool_name"] == "job_database"


def test_react_stops_duplicate_actions_and_honors_iteration_limit() -> None:
	"""重复观察或超过上限时 ReAct 必须结束，避免无限循环。"""
	intent = _intent(intent_type="job_query")
	matches = match_capabilities(intent)
	observations = [
		{
			"tool_name": "job_database",
			"arguments": {},
			"status": "completed",
			"result": {"rows": []},
		}
	]

	assert initial_react_action(matches, observations) is None
	assert select_model_action([{"name": "job_database", "arguments": {}}], matches, observations) is None
	assert can_continue(MAX_REACT_ITERATIONS, {"tool_name": "job_database", "arguments": {}, "reason": ""}) is False


def test_replanner_repairs_invalid_plan_and_preserves_completed_steps() -> None:
	"""RePlan 应识别无效能力，并基于注册表重建剩余计划。"""
	intent = _intent(
		goal="搜索 Python 岗位并评分",
		intent_type="complex_task",
		entities={"keywords": ["Python"]},
		risk_level="medium",
	)
	matches = match_capabilities(intent)
	invalid_plan = [
		{
			"id": "bad",
			"label": "不存在的能力",
			"capability": "unknown_capability",
			"depends_on": [],
			"status": "pending",
			"requires_approval": False,
		}
	]

	evaluation = evaluate_plan(invalid_plan, {match["capability"] for match in matches})
	rebuilt = rebuild_plan(intent, matches, invalid_plan)
	rebuilt_evaluation = evaluate_plan(rebuilt, {match["capability"] for match in matches})

	assert evaluation["valid"] is False
	assert evaluation["failed_step_id"] == "bad"
	assert rebuilt_evaluation["valid"] is True
	assert [step["capability"] for step in rebuilt] == ["job_collect", "job_score"]
