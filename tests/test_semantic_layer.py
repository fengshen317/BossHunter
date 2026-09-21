"""问数语义层的口径、安全和执行回归测试。"""

from pathlib import Path

import pytest

from bosshunter.assistant.semantic_layer import (
	compile_list_query,
	compile_semantic_query,
	query_semantic_database,
	semantic_catalog,
)
from bosshunter.assistant.sql_tool import _limit_rows, _validate_readonly_sql, query_database
from bosshunter.db import get_db, insert_job, update_job_score, update_job_status


def _job(job_id: str, *, city: str = "上海", industry: str = "互联网") -> dict[str, str]:
	"""创建字段完整的正式岗位测试数据。"""
	return {
		"id": job_id,
		"title": "AI Agent 工程师",
		"company": "示例科技",
		"salary": "20-30K",
		"city": city,
		"experience": "3-5年",
		"jd": "负责智能体平台研发",
		"hr_name": "招聘经理",
		"hr_title": "HR",
		"hr_active": "活跃",
		"company_size": "100-499人",
		"company_industry": industry,
		"district": "浦东新区",
		"company_background": "科技企业",
		"registered_capital": "1000万人民币",
		"url": "https://example.com/job",
	}


def test_catalog_exposes_stable_business_definitions() -> None:
	"""目录应提供指标含义、展示格式和默认正式岗位口径。"""
	catalog = semantic_catalog()

	assert catalog["metrics"]["reply_rate"]["label"] == "累计回复率"
	assert catalog["metrics"]["reply_rate"]["format"] == "percentage"
	assert catalog["dimensions"]["company_industry"]["label"] == "行业"
	assert catalog["entities"]["job"]["attributes"]["title"]["label"] == "岗位名称"
	assert catalog["concepts"]["unscored_job"]["filter"]["value"] == "pending"
	assert catalog["query_kinds"] == ["aggregate", "list"]
	assert catalog["default_filters"] == ["仅统计职位和 JD 均完整的正式岗位"]
	assert catalog["entities"]["job"]["grain"] == "每个岗位一行"
	assert catalog["entities"]["job_event"]["primary_key"] == "id"
	assert catalog["entities"]["job_history"]["attributes"]["detail"]["capabilities"] == ["select", "filter"]
	assert catalog["relationships"][0]["cardinality"] == "many_to_one"
	assert catalog["value_dictionaries"]["job.status"]["approved"] == "人工已确认，待发送"


def test_compile_query_rejects_unknown_identifier() -> None:
	"""模型不能通过计划注入任意字段或 SQL。"""
	with pytest.raises(ValueError, match="不支持的指标"):
		compile_semantic_query({"metric": "COUNT(*) FROM assistant_messages"})

	with pytest.raises(ValueError, match="过滤字段"):
		compile_semantic_query(
			{
				"metric": "job_count",
				"filters": [{"field": "api_key", "operator": "eq", "value": "secret"}],
			}
		)

	with pytest.raises(ValueError, match="未授权数据表"):
		_validate_readonly_sql("SELECT content FROM assistant_messages")


def test_compile_grouped_query_is_parameterized() -> None:
	"""实体值和行数都应作为参数传给数据库。"""
	sql, params, metadata = compile_semantic_query(
		{
			"metric": "average_score",
			"dimensions": ["company_industry"],
			"filters": [{"field": "city", "operator": "eq", "value": "上海' OR 1=1 --"}],
			"order": "desc",
			"limit": 10,
		},
	)

	assert "上海" not in sql
	assert params == ["上海' OR 1=1 --", 10]
	assert "GROUP BY" in sql
	assert metadata["definition"] == "已完成 AI 评分岗位的平均匹配分"


def test_compile_list_query_is_parameterized_and_rejects_unknown_fields() -> None:
	"""岗位明细查询应使用字段白名单、参数化过滤和受控排序。"""
	sql, params, metadata = compile_list_query(
		{
			"query_kind": "list",
			"entity": "job",
			"fields": ["id", "title", "company", "status"],
			"filters": [{"field": "status", "operator": "eq", "value": "pending"}],
			"order_by": [{"field": "created_at", "direction": "desc"}],
			"limit": 50,
		}
	)

	assert "SELECT j.id AS id, j.title AS title" in sql
	assert "COUNT(*) OVER() AS __total_count" in sql
	assert "j.status = ?" in sql
	assert "ORDER BY j.created_at DESC LIMIT ?" in sql
	assert params == ["pending", 50]
	assert metadata["query_kind"] == "list"
	with pytest.raises(ValueError, match="明细字段"):
		compile_list_query({"query_kind": "list", "entity": "job", "fields": ["api_key"]})


def test_semantic_list_limit_preserves_real_total_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	"""列表只返回有限明细，但总数必须覆盖全部符合条件的岗位。"""
	conn = get_db(tmp_path / "limited-list.db")
	try:
		for index in range(25):
			insert_job(conn, _job(f"job-{index}"))
		monkeypatch.setattr(
			"bosshunter.assistant.semantic_layer.plan_semantic_query",
			lambda question, config, goal="", feedback="": {
				"query_kind": "list",
				"entity": "job",
				"fields": ["id", "title", "company", "status"],
				"filters": [],
				"order_by": [{"field": "created_at", "direction": "desc"}],
				"limit": 20,
			},
		)

		payload = query_semantic_database(conn, "我现在有哪些岗位", {})
	finally:
		conn.close()

	assert payload["row_count"] == 25
	assert payload["total_count"] == 25
	assert payload["returned_count"] == 20
	assert payload["limit"] == 20
	assert payload["has_more"] is True
	assert len(payload["rows"]) == 20
	assert all("__total_count" not in row for row in payload["rows"])


def test_compile_event_detail_query_uses_declared_relationship_and_grain() -> None:
	"""流程事件明细应通过受控关系补充岗位信息，并保持一事件一行。"""
	sql, params, metadata = compile_list_query(
		{
			"query_kind": "list",
			"entity": "job_event",
			"fields": ["id", "job_id", "job_title", "event_type", "event_time"],
			"filters": [{"field": "event_type", "operator": "eq", "value": "hr_replied"}],
			"order_by": [{"field": "event_time", "direction": "desc"}],
			"limit": 20,
		}
	)

	assert "FROM job_events e JOIN jobs j ON j.id = e.job_id" in sql
	assert "e.event_type = ?" in sql
	assert "ORDER BY e.event_time DESC LIMIT ?" in sql
	assert params == ["hr_replied", 20]
	assert metadata["grain"] == "每次岗位流程事件一行"
	assert metadata["primary_key"] == "id"


def test_compile_history_detail_query_enforces_field_capabilities() -> None:
	"""只允许展示和过滤的长文本详情字段不得被模型用于排序。"""
	with pytest.raises(ValueError, match="排序字段或方向"):
		compile_list_query(
			{
				"query_kind": "list",
				"entity": "job_history",
				"fields": ["job_id", "action", "detail"],
				"order_by": [{"field": "detail", "direction": "asc"}],
			}
		)


def test_semantic_query_calculates_reply_rate_and_excludes_placeholder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	"""累计回复率应排除 HR 主动沟通占位记录，并按发送阶段口径计算。"""
	db_path = tmp_path / "semantic.db"
	conn = get_db(db_path)
	try:
		for job_id, status in (("sent", "sent"), ("replied", "replied")):
			insert_job(conn, _job(job_id))
			update_job_score(conn, job_id, 85, "匹配")
			update_job_status(conn, job_id, status)
		insert_job(conn, {**_job("placeholder"), "title": "HR主动沟通", "jd": ""})
		update_job_status(conn, "placeholder", "replied")
		monkeypatch.setattr(
			"bosshunter.assistant.semantic_layer.plan_semantic_query",
			lambda question, config, goal="", feedback="": {"metric": "reply_rate", "dimensions": [], "filters": []},
		)

		payload = query_semantic_database(conn, "回复率是多少", {})
	finally:
		conn.close()

	assert payload["mode"] == "semantic"
	assert payload["rows"] == [{"reply_rate": 50.0}]
	assert payload["metric"]["time_field"] == "created_at"


def test_status_changes_create_structured_events_and_funnel(tmp_path: Path) -> None:
	"""状态流转应自动留下结构化事件，分析不依赖可变的当前状态。"""
	conn = get_db(tmp_path / "events.db")
	try:
		insert_job(conn, _job("event-job"))
		update_job_status(conn, "event-job", "scored")
		update_job_status(conn, "event-job", "approved")
		update_job_status(conn, "event-job", "sent")
		events = conn.execute(
			"SELECT event_type, from_status, to_status FROM job_events WHERE job_id = ? ORDER BY id",
			("event-job",),
		).fetchall()
		funnel = conn.execute("SELECT * FROM semantic_job_funnel WHERE job_id = ?", ("event-job",)).fetchone()
	finally:
		conn.close()

	assert [row["event_type"] for row in events] == ["job_collected", "ai_scored", "user_approved", "greeting_sent"]
	assert events[-1]["from_status"] == "approved"
	assert events[-1]["to_status"] == "sent"
	assert funnel["scored_at"] is not None
	assert funnel["approved_at"] is not None
	assert funnel["sent_at"] is not None


def test_event_metric_uses_event_time_instead_of_current_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	"""期间发送数应读取事件时间，即使岗位后来已经进入回复阶段。"""
	conn = get_db(tmp_path / "event-metric.db")
	try:
		insert_job(conn, _job("later-replied"))
		update_job_status(conn, "later-replied", "approved")
		update_job_status(conn, "later-replied", "sent")
		update_job_status(conn, "later-replied", "replied")
		monkeypatch.setattr(
			"bosshunter.assistant.semantic_layer.plan_semantic_query",
			lambda question, config, goal="", feedback="": {
				"metric": "sent_event_job_count",
				"dimensions": [],
				"filters": [],
				"time_range": {"preset": "last_7_days"},
			},
		)
		payload = query_semantic_database(conn, "最近7天发送了多少岗位", {})
	finally:
		conn.close()

	assert payload["rows"] == [{"sent_event_job_count": 1}]
	assert payload["metric"]["time_field"] == "event_time"


def test_query_database_routes_aggregate_question_to_semantic_layer(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""现有助手入口应优先使用受控语义计划，而不是让模型生成聚合 SQL。"""
	db_path = tmp_path / "assistant-semantic.db"
	conn = get_db(db_path)
	try:
		insert_job(conn, _job("semantic-route"))
	finally:
		conn.close()
	monkeypatch.setattr(
		"bosshunter.assistant.semantic_layer.plan_semantic_query",
		lambda question, config, goal="", feedback="": {"metric": "job_count", "dimensions": ["city"], "filters": []},
	)

	payload = query_database(db_path, "各城市分别有多少岗位？", {})

	assert payload["mode"] == "semantic"
	assert payload["rows"] == [{"city": "上海", "job_count": 1}]
	assert payload["metric"]["metric_label"] == "岗位数"
	assert "reply_rate" in payload["schema"]["metrics"]
	assert "assistant_messages" not in payload["schema"]


def test_query_database_routes_unscored_job_list_to_semantic_layer(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""“哪些岗位未评分”应走岗位明细语义计划，而不是执行评分或自由生成 SQL。"""
	db_path = tmp_path / "assistant-semantic-list.db"
	conn = get_db(db_path)
	try:
		insert_job(conn, _job("pending-job"))
		insert_job(conn, _job("scored-job"))
		update_job_score(conn, "scored-job", 88, "匹配")
		update_job_status(conn, "scored-job", "ready")
	finally:
		conn.close()
	monkeypatch.setattr(
		"bosshunter.assistant.semantic_layer.plan_semantic_query",
		lambda question, config, goal="", feedback="": {
			"query_kind": "list",
			"entity": "job",
			"fields": ["id", "title", "company", "status"],
			"filters": [{"field": "status", "operator": "eq", "value": "pending"}],
			"order_by": [{"field": "created_at", "direction": "desc"}],
			"limit": 100,
		},
	)

	payload = query_database(db_path, "看下哪些岗位还没有评分", {})

	assert payload["mode"] == "semantic"
	assert payload["semantic"]["query_kind"] == "list"
	assert payload["rows"] == [
		{"id": "pending-job", "title": "AI Agent 工程师", "company": "示例科技", "status": "pending"}
	]


def test_compile_time_presets_use_database_clock() -> None:
	"""时间过滤应使用数据库端时钟表达式，且不把时刻作为参数由应用侧计算。"""
	sql_today, params_today, _ = compile_semantic_query(
		{"metric": "collected_job_count", "time_range": {"preset": "today"}}
	)
	assert "e.event_time >= date('now')" in sql_today
	assert params_today == ["job_collected"]

	sql_today_jobs, _, _ = compile_semantic_query({"metric": "job_count", "time_range": {"preset": "today"}})
	assert "j.created_at >= date('now')" in sql_today_jobs

	sql_90, params_90, _ = compile_semantic_query(
		{"metric": "job_count", "time_range": {"preset": "last_90_days"}}
	)
	assert "j.created_at >= datetime('now', '-90 days')" in sql_90
	assert params_90 == []

	with pytest.raises(ValueError, match="时间范围"):
		compile_semantic_query({"metric": "job_count", "time_range": {"preset": "last_year"}})


def test_semantic_list_retries_once_on_empty_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	"""list 查询为空时应带着观察重新规划一次，并保留两次尝试血缘。"""
	conn = get_db(tmp_path / "retry.db")
	plans = [
		{
			"query_kind": "list",
			"entity": "job",
			"fields": ["id", "title"],
			"filters": [{"field": "city", "operator": "eq", "value": "不存在城市"}],
			"limit": 50,
		},
		{
			"query_kind": "list",
			"entity": "job",
			"fields": ["id", "title"],
			"filters": [],
			"limit": 50,
		},
	]
	seen: list[tuple[str, str]] = []

	def fake_plan(question, config, goal="", feedback=""):
		seen.append((goal, feedback))
		return plans[len(seen) - 1]

	try:
		insert_job(conn, _job("retry-job"))
		monkeypatch.setattr("bosshunter.assistant.semantic_layer.plan_semantic_query", fake_plan)

		payload = query_semantic_database(conn, "有哪些岗位", {}, goal="统计北京的岗位")
	finally:
		conn.close()

	assert payload["row_count"] == 1
	assert [item["status"] for item in payload["query"]["attempts"]] == ["empty", "completed"]
	assert seen[0] == ("统计北京的岗位", "")
	assert "返回 0 行" in seen[1][1]


def test_goal_hint_is_passed_to_planner_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
	"""多轮合并目标应写入语义规划提示词，消解“那上海呢”类指代。"""
	prompts: list[str] = []

	def fake_call(prompt, config):
		prompts.append(prompt)
		return '{"query_kind":"unsupported","reason":"目录不足"}'

	monkeypatch.setattr("bosshunter.assistant.semantic_layer.call_anthropic_text", fake_call)

	try:
		compile_semantic_query(
			{"metric": "job_count", "time_range": {"preset": "last_7_days"}}
		)
	except ValueError:
		pass

	from bosshunter.assistant.semantic_layer import plan_semantic_query

	try:
		plan_semantic_query("那上海呢？", {}, goal="统计北京和上海的岗位总数")
	except ValueError:
		pass

	assert "统计北京和上海的岗位总数" in prompts[-1]


def test_limit_rows_strips_existing_limit_and_keeps_offset() -> None:
	"""强制 LIMIT 应正确处理已带 LIMIT/OFFSET 的查询，不产生语法错误。"""
	assert _limit_rows("SELECT * FROM jobs LIMIT 5").endswith("LIMIT 200")
	assert _limit_rows("SELECT * FROM jobs LIMIT 5 OFFSET 10").endswith("LIMIT 200 OFFSET 10")
	assert _limit_rows("SELECT * FROM jobs OFFSET 10").endswith("LIMIT 200 OFFSET 10")
	assert _limit_rows("SELECT * FROM jobs").endswith("LIMIT 200")


def test_recursive_cte_is_rejected() -> None:
	"""递归 CTE 可构造无界计算，问数白名单应显式拒绝。"""
	with pytest.raises(ValueError, match="WITH RECURSIVE"):
		_validate_readonly_sql("WITH RECURSIVE cnt AS (SELECT 1) SELECT * FROM cnt")


def test_query_database_accepts_truly_empty_result_after_one_retry(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""真实为空的数据不应烧满三次 SQL 重试，第二次空结果即接受。"""
	db_path = tmp_path / "empty.db"
	conn = get_db(db_path)
	try:
		insert_job(conn, _job("only-job"))
	finally:
		conn.close()
	plans: list[dict] = []

	def fake_call(prompt, config):
		plans.append({"prompt": prompt})
		return '{"sql":"SELECT * FROM jobs WHERE city LIKE ?","params":["%不存在%"]}'

	monkeypatch.setattr("bosshunter.assistant.sql_tool.call_anthropic_text", fake_call)

	payload = query_database(db_path, "查找不存在城市的岗位", {})

	assert payload["row_count"] == 0
	assert len(plans) == 2
	assert "用户多轮合并目标" in plans[1]["prompt"]
