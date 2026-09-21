"""问答 Agent 证据域隔离回归测试。"""

from typing import Any

from bosshunter.assistant.evidence import build_evidence_result
from bosshunter.assistant.evidence_planning import build_evidence_tasks, evidence_tasks_to_plan
from bosshunter.assistant.evidence_router import allowed_tool_names, route_evidence
from bosshunter.assistant.evidence_synthesis import (
	build_evidence_context,
	evaluate_evidence_coverage,
	evidence_citations,
)
from bosshunter.assistant.executors import execute_analytics, execute_private_documents, execute_web_research
from bosshunter.assistant.models import IntentResult


def _intent(intent_type: str = "general_question", **overrides: Any) -> IntentResult:
	"""创建最小完整意图。"""
	result: IntentResult = {
		"goal": "回答用户问题",
		"intent_type": intent_type,  # type: ignore[typeddict-item]
		"entities": {},
		"constraints": {},
		"missing_fields": [],
		"confidence": 0.95,
		"risk_level": "low",
		"reason": "测试",
	}
	result.update(overrides)
	return result


def test_direct_question_does_not_enable_any_read_tool() -> None:
	"""纯知识问答不应访问数据库、简历或公开网页。"""
	route = route_evidence("什么是岗位漏斗？", _intent())

	assert route["mode"] == "direct"
	assert allowed_tool_names(route, web_enabled=True) == set()


def test_analytics_question_only_enables_database() -> None:
	"""统计问题只能进入结构化问数域。"""
	route = route_evidence("最近采集了多少个岗位？", _intent("job_query"))

	assert route["mode"] == "structured_data"
	assert allowed_tool_names(route, web_enabled=True) == {"job_database"}


def test_resume_question_only_enables_private_document() -> None:
	"""简历分析不应自动查询岗位库或联网。"""
	route = route_evidence("分析一下我的简历优势", _intent())

	assert route["mode"] == "private_document"
	assert allowed_tool_names(route, web_enabled=True) == {"resume_read"}


def test_current_company_question_only_enables_web_domain() -> None:
	"""公司最新事实应使用公开资料，不应读取简历和岗位统计。"""
	route = route_evidence("腾讯最近有什么新闻？", _intent())

	assert route["mode"] == "public_current"
	assert route["freshness_required"] is True
	assert allowed_tool_names(route, web_enabled=True) == {"web_search", "company_research"}
	assert allowed_tool_names(route, web_enabled=False) == set()


def test_mixed_question_keeps_domains_explicit() -> None:
	"""跨领域问题应显式声明每个证据域，而不是混成一个工具池。"""
	route = route_evidence("最近哪些岗位适合我的简历，这些公司怎么样？", _intent("job_query"))

	assert route["mode"] == "mixed"
	assert route["domains"] == ["structured_data", "private_document", "public_current"]


def test_evidence_result_has_shared_traceable_contract() -> None:
	"""不同执行器结果必须转换成统一、可追溯的证据协议。"""
	result = build_evidence_result(
		"query-jobs",
		"analytics",
		"structured_data",
		{"row_count": 3, "rows": []},
	)

	assert result["status"] == "completed"
	assert result["executor"] == "analytics"
	assert result["evidence"][0]["source_id"] == "business_database"
	assert result["evidence"][0]["value"] == 3


def test_mixed_evidence_plan_runs_data_then_private_then_web() -> None:
	"""混合问题必须先收敛本地候选，再研究公开信息。"""
	route = route_evidence("最近哪些岗位适合我的简历，这些公司怎么样？", _intent("job_query"))

	tasks = build_evidence_tasks("最近哪些岗位适合我的简历，这些公司怎么样？", route)
	plan = evidence_tasks_to_plan(tasks)

	assert [task["evidence_type"] for task in tasks] == [
		"structured_data",
		"private_document",
		"public_current",
	]
	assert tasks[0]["depends_on"] == []
	assert tasks[1]["depends_on"] == [tasks[0]["id"]]
	assert tasks[2]["depends_on"] == [tasks[1]["id"]]
	assert [step["capability"] for step in plan] == ["job_query", "resume_read", "web_search"]


def test_evidence_synthesis_reports_missing_required_task() -> None:
	"""综合器应明确指出未完成的必需证据，而不是让模型自行补全。"""
	route = route_evidence("最近哪些岗位适合我的简历？", _intent("job_query"))
	tasks = build_evidence_tasks("最近哪些岗位适合我的简历？", route)
	results = [
		build_evidence_result(
			tasks[0]["id"],
			"analytics",
			"structured_data",
			{"row_count": 2, "rows": [{"title": "Agent 工程师"}]},
		)
	]

	coverage = evaluate_evidence_coverage(tasks, results)

	assert coverage["complete"] is False
	assert coverage["missing_task_ids"] == [tasks[1]["id"]]


def test_evidence_context_is_bounded_and_preserves_sources() -> None:
	"""统一上下文应限制结果规模，同时保留数据库和网页来源。"""
	analytics = build_evidence_result(
		"query",
		"analytics",
		"structured_data",
		{"row_count": 80, "rows": [{"id": index} for index in range(80)]},
	)
	web = build_evidence_result(
		"research",
		"web_research",
		"public_current",
		{"sources": [{"title": "公司官网", "url": "https://example.com", "excerpt": "公开资料"}]},
	)

	context = build_evidence_context([analytics, web])
	citations = evidence_citations([analytics, web])

	assert len(analytics["data"]["rows"]) == 50
	assert "https://example.com" in context
	assert {item["source"] for item in citations} == {"structured_data", "public_current"}


def test_private_executor_selects_only_bounded_candidates() -> None:
	"""私有资料执行器应基于简历和前序岗位生成少量入围对象。"""
	seen_prompt = ""

	def model_call(prompt: str, config: dict[str, Any]) -> str:
		"""返回超过上限且包含重复公司的匹配结果。"""
		nonlocal seen_prompt
		seen_prompt = prompt
		assert config == {"ai": {}}
		return """{
"analysis":"优先考虑 Agent 岗位",
"selected_jobs":[
  {"id":"1","title":"Agent工程师","company":"甲","reason":"匹配"},
  {"id":"2","title":"后端工程师","company":"乙","reason":"匹配"}
],
"selected_companies":["甲","乙","甲"]
}"""

	result = execute_private_documents(
		"哪些岗位适合我？",
		"# 简历\nPython 后端经验",
		{"query": {"rows": [{"id": "1", "company": "甲"}]}},
		{"ai": {}},
		model_call,
	)

	assert "Python 后端经验" in seen_prompt
	assert result["selected_companies"] == ["甲", "乙"]
	assert len(result["selected_jobs"]) == 2


def test_web_executor_only_researches_selected_companies() -> None:
	"""联网执行器应消费私有匹配结果，不得扩散到全部候选公司。"""
	queries: list[str] = []

	def search(query: str) -> list[dict[str, str]]:
		"""记录最终联网查询。"""
		queries.append(query)
		return [{"title": "官网", "url": "https://example.com", "excerpt": "资料"}]

	result = execute_web_research(
		"这些公司最近怎么样？",
		{"matching": {"selected_companies": ["甲公司", "乙公司", "丙公司"]}},
		search,
		max_results=2,
	)

	assert result["researched_companies"] == ["甲公司", "乙公司"]
	assert "甲公司、乙公司" in queries[0]
	assert "丙公司" not in queries[0]


def test_analytics_executor_does_not_cross_domain_on_empty_result() -> None:
	"""问数执行器只返回查询结果，不自行降级到联网或私有资料。"""
	calls: list[str] = []

	def query(question: str) -> dict[str, Any]:
		calls.append(question)
		return {"row_count": 0, "rows": []}

	result = execute_analytics("最近有多少岗位？", query)

	assert calls == ["最近有多少岗位？"]
	assert result == {"row_count": 0, "rows": []}
