"""LangGraph 求职助手的回归测试。"""

import json
from pathlib import Path
from unittest.mock import patch

import yaml

from bosshunter.assistant.repository import (
	create_bad_case,
	list_bad_cases,
	list_memories,
	list_messages,
	rate_assistant_message,
)
from bosshunter.assistant.graph import (
	IntentResult,
	_build_pending_action,
	_execution_plan,
	_fallback_action_tool,
	_job_context,
)
from bosshunter.assistant.service import AssistantService
from bosshunter.db import get_db, insert_job


def _prepare_base_dir(tmp_path: Path) -> Path:
	"""创建最小可运行的助手目录。"""
	config = {
		"profile": {"resume_path": "./resume.md"},
		"ai": {"provider": "anthropic", "model": "test-model", "api_key": "test"},
	}
	(tmp_path / "config.yaml").write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
	(tmp_path / "resume.md").write_text("# 项目经历\n使用 FastAPI 开发求职 Agent。", encoding="utf-8")
	db = get_db(tmp_path / "data" / "bosshunter.db")
	try:
		insert_job(
			db,
			{
				"id": "job-1",
				"title": "AI Agent 工程师",
				"company": "示例科技",
				"salary": "20-30K",
				"city": "北京",
				"experience": "3-5年",
				"jd": "负责 LangGraph Agent 开发",
				"hr_name": "王女士",
				"hr_title": "HR",
				"hr_active": "活跃",
				"company_size": "100-499人",
				"company_industry": "互联网",
				"registered_capital": "5000万人民币",
				"url": "https://example.com/job-1",
			},
		)
	finally:
		db.close()
	return tmp_path


def test_intent_result_defines_the_understanding_layer_contract() -> None:
	"""理解节点的输出应具有固定字段，供后续路由安全消费。"""
	result: IntentResult = {
		"goal": "搜索 Python 岗位",
		"intent_type": "job_search",
		"entities": {"keywords": ["Python"]},
		"constraints": {},
		"missing_fields": ["cities"],
		"confidence": 0.92,
		"risk_level": "low",
		"reason": "用户明确请求搜索 Python 岗位。",
	}

	assert set(IntentResult.__required_keys__) == set(result)
	assert result["intent_type"] == "job_search"


def test_assistant_persists_multi_turn_messages_and_explicit_memory(tmp_path: Path) -> None:
	"""多轮消息和用户明确记忆应分别持久化。"""
	base_dir = _prepare_base_dir(tmp_path)
	service = AssistantService(base_dir, base_dir / "config.yaml")
	conversation_id = service.ensure_conversation()
	try:
		with (
			patch("bosshunter.assistant.graph.call_ai_with_tools", return_value={"content": "", "tool_calls": []}),
			patch("bosshunter.assistant.graph.call_anthropic_text", return_value="数据库中有一个 Agent 岗位。"),
			patch(
				"bosshunter.assistant.sql_tool._plan_query",
				return_value=("SELECT * FROM jobs ORDER BY updated_at DESC", []),
			),
		):
			first = service.send_message(conversation_id, "分析当前岗位")
			second = service.send_message(conversation_id, "记住：我只考虑北京岗位")

		assert first["status"] == "completed"
		assert second["status"] == "completed"
		assert first["intent"]["intent_type"] == "general_question"
		assert first["result_summary"]["status"] == "completed"
		db = get_db(base_dir / "data" / "bosshunter.db")
		try:
			messages = list_messages(db, conversation_id)
			memories = list_memories(db)
		finally:
			db.close()
		assert [item["role"] for item in messages] == ["user", "assistant", "user", "assistant"]
		assert any("只考虑北京岗位" in item["content"] for item in memories)
	finally:
		service.close()


def test_readonly_job_query_runs_through_react_observation_loop(tmp_path: Path) -> None:
	"""只读岗位查询应执行 ReAct 工具观察，并在获得结果后停止循环。"""
	base_dir = _prepare_base_dir(tmp_path)
	service = AssistantService(base_dir, base_dir / "config.yaml")
	conversation_id = service.ensure_conversation()
	execution_events: list[dict[str, str]] = []
	intent_json = json.dumps(
		{
			"goal": "查询当前岗位数据",
			"intent_type": "job_query",
			"entities": {},
			"constraints": {},
			"missing_fields": [],
			"confidence": 0.95,
			"risk_level": "low",
			"reason": "用户明确要求查询岗位。",
		},
		ensure_ascii=False,
	)
	try:
		with (
			patch("bosshunter.assistant.graph.call_anthropic_text", return_value=intent_json),
			patch(
				"bosshunter.assistant.graph.call_ai_with_tools",
				return_value={"content": "已根据岗位数据完成分析。", "tool_calls": []},
			),
			patch(
				"bosshunter.assistant.sql_tool._plan_query",
				return_value=("SELECT * FROM jobs ORDER BY updated_at DESC", []),
			),
		):
			result = service.send_message(conversation_id, "查询当前岗位数据", on_event=execution_events.append)

		assert result["status"] == "completed"
		assert result["message"]["content"] == "已根据岗位数据完成分析。"
		assert result["capabilities"] == ["job_query"]
		assert result["result_summary"]["react_iterations"] == 1
		assert any(item == {"name": "job_database", "status": "completed"} for item in result["tool_trace"])
		assert len(execution_events) > len(result["execution_events"])
		assert len({event["id"] for event in result["execution_events"]}) == len(result["execution_events"])
		assert any(event["id"] == "intent" and event["status"] == "completed" for event in execution_events)
		assert any(event["id"].endswith("job_database") for event in execution_events)
		db = get_db(base_dir / "data" / "bosshunter.db")
		try:
			persisted = list_messages(db, conversation_id)[-1]
		finally:
			db.close()
		assert persisted["metadata"]["execution_events"] == execution_events
	finally:
		service.close()


def test_hr_reply_uses_langgraph_interrupt_and_resume(tmp_path: Path) -> None:
	"""HR 回复草稿必须暂停，只有结构化确认后才能恢复。"""
	base_dir = _prepare_base_dir(tmp_path)
	service = AssistantService(base_dir, base_dir / "config.yaml")
	conversation_id = service.ensure_conversation()
	try:
		with (
			patch("bosshunter.assistant.graph.call_ai_with_tools", return_value={"content": "", "tool_calls": []}),
			patch("bosshunter.assistant.graph._generate_reply_from_text", return_value="您好，我有相关项目经验。"),
		):
			result = service.send_message(
				conversation_id,
				"HR 问我有没有 LangGraph 项目经验，怎么回复？",
				job_id="job-1",
			)

		assert result["status"] == "interrupted"
		assert result["approval"]["type"] == "reply_draft_approval"
		resumed = service.resume(conversation_id, {"action": "approve"})
		assert resumed["status"] == "completed"
		assert "不会自动发送" in resumed["message"]["content"]
	finally:
		service.close()


def test_job_context_exposes_full_schema_and_job_fields(tmp_path: Path) -> None:
	"""岗位上下文应包含完整字段，避免 Agent 因列白名单遗漏信息。"""
	base_dir = _prepare_base_dir(tmp_path)
	db = get_db(base_dir / "data" / "bosshunter.db")
	try:
		context, _ = _job_context(db, "查看岗位详情", "job-1")
	finally:
		db.close()

	payload = json.loads(context)
	assert "registered_capital" in payload["schema"]["jobs"]
	assert payload["rows"][0]["company_industry"] == "互联网"


def test_job_context_plans_entity_aware_readonly_sql(tmp_path: Path) -> None:
	"""岗位查询工具应把用户实体转换成参数化只读 SQL。"""
	base_dir = _prepare_base_dir(tmp_path)
	db = get_db(base_dir / "data" / "bosshunter.db")
	plan = json.dumps(
		{
			"sql": "SELECT * FROM jobs WHERE registered_capital LIKE ?",
			"params": ["%5000万%"],
		},
		ensure_ascii=False,
	)
	try:
		with patch("bosshunter.assistant.sql_tool.call_anthropic_text", return_value=plan):
			context, _ = _job_context(db, "查找注册资本在5000万以上的公司", None, {"ai": {}})
	finally:
		db.close()

	payload = json.loads(context)
	assert payload["query"]["params"] == ["%5000万%"]
	assert payload["row_count"] == 1
	assert payload["rows"][0]["company"] == "示例科技"


def test_common_chat_actions_have_deterministic_tool_fallbacks() -> None:
	"""常用动作不能因为模型漏发 tool call 而退化成普通问答。"""
	assert _fallback_action_tool("开始为待处理岗位评分")["name"] == "score_jobs"
	assert _fallback_action_tool("为已评分岗位生成招呼语")["name"] == "generate_greetings"
	assert _fallback_action_tool("启动监测回复")["name"] == "monitor_replies"
	collect = _fallback_action_tool("采集 Python Agent 岗位")
	assert collect == {"name": "collect_jobs", "arguments": {"keywords": ["Python Agent"]}}


def test_generate_greetings_action_requires_approval_and_exposes_plan(tmp_path: Path) -> None:
	"""生成招呼语应先形成审批提案，并提供可展示的执行规划。"""
	base_dir = _prepare_base_dir(tmp_path)
	db = get_db(base_dir / "data" / "bosshunter.db")
	try:
		db.execute("UPDATE jobs SET status = 'ready', greeting = '' WHERE id = 'job-1'")
		db.commit()
		action = _build_pending_action(db, [{"name": "generate_greetings", "arguments": {}}], None)
	finally:
		db.close()

	assert action is not None
	assert action["type"] == "generate_greetings"
	assert action["allowed_actions"] == ["approve", "reject"]
	plan = _execution_plan(action["type"])
	assert [step["status"] for step in plan] == ["completed", "approval_required", "pending"]


def test_score_action_excludes_manual_and_incomplete_records(tmp_path: Path) -> None:
	"""主动沟通占位记录和缺少 JD 的记录不得进入评分审批。"""
	base_dir = _prepare_base_dir(tmp_path)
	db = get_db(base_dir / "data" / "bosshunter.db")
	try:
		db.execute("UPDATE jobs SET jd = '' WHERE id = 'job-1'")
		insert_job(
			db,
			{
				"id": "manual-chat",
				"title": "HR主动沟通",
				"company": "主动联系公司",
				"salary": "",
				"city": "",
				"experience": "",
				"jd": "",
				"hr_name": "",
				"hr_title": "",
				"hr_active": "",
				"company_size": "",
				"company_industry": "",
				"url": "",
			},
		)
		action = _build_pending_action(db, [{"name": "score_jobs", "arguments": {}}], None)
	finally:
		db.close()

	assert action is None


def test_manual_record_can_be_completed_before_scoring(tmp_path: Path) -> None:
	"""补充真实职位和 JD 后应形成明确的人工审批提案。"""
	base_dir = _prepare_base_dir(tmp_path)
	db = get_db(base_dir / "data" / "bosshunter.db")
	try:
		db.execute("UPDATE jobs SET id = 'manual-job', title = 'HR主动沟通', jd = '', salary = '' WHERE id = 'job-1'")
		db.commit()
		action = _build_pending_action(
			db,
			[
				{
					"name": "complete_job_details",
					"arguments": {
						"job_id": "manual-job",
						"title": "AI Agent 工程师",
						"jd": "负责智能体应用开发",
						"salary": "20-30K",
					},
				}
			],
			None,
		)
	finally:
		db.close()

	assert action is not None
	assert action["type"] == "complete_job_details"
	assert action["parameters"]["jd"] == "负责智能体应用开发"


def test_confirmed_job_details_are_promoted_and_scoring_starts(tmp_path: Path) -> None:
	"""审批后应落库完整信息，并只为该岗位启动评分任务。"""
	from bosshunter.web import server

	base_dir = _prepare_base_dir(tmp_path)
	db = get_db(base_dir / "data" / "bosshunter.db")
	try:
		db.execute("UPDATE jobs SET id = 'manual-job', title = 'HR主动沟通', jd = '', salary = '', status = 'manual'")
		db.commit()
	finally:
		db.close()
	server.set_base_dir(base_dir)
	try:
		with patch.object(server.task_runner, "start", return_value={"id": "task-1", "mode": "score"}) as start:
			result = server._start_assistant_action(
				{
					"type": "complete_job_details",
					"parameters": {
						"job_id": "manual-job",
						"title": "AI Agent 工程师",
						"jd": "负责智能体应用开发",
						"salary": "20-30K",
						"url": "https://example.com/job",
					},
				}
			)
		db = get_db(base_dir / "data" / "bosshunter.db")
		try:
			row = db.execute("SELECT title, jd, salary, url, status FROM jobs WHERE id = 'manual-job'").fetchone()
		finally:
			db.close()
	finally:
		server.set_base_dir(Path.cwd())

	assert result["mode"] == "score"
	assert tuple(row) == ("AI Agent 工程师", "负责智能体应用开发", "20-30K", "https://example.com/job", "pending")
	assert start.call_args.args[0] == "score"
	assert start.call_args.args[1]["_workbench_job_ids"] == ["manual-job"]


def test_sql_tool_reacts_to_execution_error_and_replans(tmp_path: Path) -> None:
	"""SQL 字段错误后应把观察反馈给模型并执行修正查询。"""
	base_dir = _prepare_base_dir(tmp_path)
	bad_plan = json.dumps({"sql": "SELECT missing_column FROM jobs", "params": []})
	good_plan = json.dumps({"sql": "SELECT * FROM jobs WHERE company LIKE ?", "params": ["%示例%"]})
	with patch("bosshunter.assistant.sql_tool.call_anthropic_text", side_effect=[bad_plan, good_plan]) as model:
		from bosshunter.assistant.sql_tool import query_database

		payload = query_database(base_dir / "data" / "bosshunter.db", "查找示例公司的岗位", {})

	assert payload["row_count"] == 1
	assert [item["status"] for item in payload["query"]["attempts"]] == ["failed", "completed"]
	assert "missing_column" in model.call_args_list[1].args[0]


def test_sql_tool_reacts_to_empty_result_and_broadens_query(tmp_path: Path) -> None:
	"""首次查询为空时应允许模型反思过滤条件并再次查询。"""
	base_dir = _prepare_base_dir(tmp_path)
	empty_plan = json.dumps({"sql": "SELECT * FROM jobs WHERE company = ?", "params": ["不存在"]})
	good_plan = json.dumps({"sql": "SELECT * FROM jobs WHERE title LIKE ?", "params": ["%Agent%"]})
	with patch("bosshunter.assistant.sql_tool.call_anthropic_text", side_effect=[empty_plan, good_plan]):
		from bosshunter.assistant.sql_tool import query_database

		payload = query_database(base_dir / "data" / "bosshunter.db", "查找 Agent 岗位", {})

	assert payload["row_count"] == 1
	assert [item["status"] for item in payload["query"]["attempts"]] == ["empty", "completed"]


def test_bad_case_captures_user_question_intent_and_routing_snapshot(tmp_path: Path) -> None:
	"""点踩补充应形成可直接用于意图和路由优化的完整样本。"""
	base_dir = _prepare_base_dir(tmp_path)
	db = get_db(base_dir / "data" / "bosshunter.db")
	try:
		from bosshunter.assistant.repository import add_message, create_conversation

		conversation = create_conversation(db)
		user_message = add_message(db, conversation["id"], "user", "只看一下当前岗位")
		assistant_message = add_message(
			db,
			conversation["id"],
			"assistant",
			"我来启动岗位采集。",
			metadata={
				"intent": {"intent_type": "job_search", "confidence": 0.62},
				"capabilities": ["collect_jobs"],
				"result_summary": {"status": "approval_required"},
			},
		)
		created = create_bad_case(
			db,
			conversation["id"],
			assistant_message["id"],
			"routing_error",
			"job_query",
			"只查询数据库，不要启动采集。",
		)
		items = list_bad_cases(db)
	finally:
		db.close()

	assert created["category"] == "routing_error"
	assert items[0]["user_message_id"] == user_message["id"]
	assert items[0]["user_message"] == "只看一下当前岗位"
	assert items[0]["intent"]["intent_type"] == "job_search"
	assert items[0]["routing"]["capabilities"] == ["collect_jobs"]


def test_message_rating_can_be_changed_between_good_and_bad(tmp_path: Path) -> None:
	"""同一回答的手势可修改，并始终只保留一条当前评价。"""
	base_dir = _prepare_base_dir(tmp_path)
	db = get_db(base_dir / "data" / "bosshunter.db")
	try:
		from bosshunter.assistant.repository import add_message, create_conversation

		conversation = create_conversation(db)
		message = add_message(db, conversation["id"], "assistant", "回答")
		rate_assistant_message(db, conversation["id"], message["id"], "helpful")
		result = rate_assistant_message(db, conversation["id"], message["id"], "unhelpful")
		row = db.execute(
			"SELECT rating FROM assistant_message_ratings WHERE assistant_message_id = ?",
			(message["id"],),
		).fetchone()
	finally:
		db.close()

	assert result["rating"] == "unhelpful"
	assert row["rating"] == "unhelpful"
