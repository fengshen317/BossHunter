"""岗位数据库的自然语言只读 SQL 查询工具。"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from bosshunter.ai.credentials import call_anthropic_text
from bosshunter.assistant.semantic_layer import looks_like_semantic_question, query_semantic_database, semantic_catalog
from bosshunter.db import get_db


_FORBIDDEN_SQL = re.compile(
	r"\b(?:INSERT|UPDATE|DELETE|REPLACE|DROP|ALTER|CREATE|ATTACH|DETACH|PRAGMA|VACUUM|REINDEX|ANALYZE)\b",
	re.IGNORECASE,
)
_MAX_QUERY_ATTEMPTS = 3
_QUERYABLE_RELATIONS = {"jobs", "history", "job_events", "semantic_job_current", "semantic_job_funnel"}
# 模型生成的 SQL 强制附加的最大行数上限，防止整表（含完整 JD）涌入 LLM 上下文。
_MAX_QUERY_ROWS = 200


_LIMIT_TAIL = re.compile(r"\bLIMIT\s+(\d+)(?:\s+OFFSET\s+(\d+))?\s*$", re.IGNORECASE)
_OFFSET_TAIL = re.compile(r"\bOFFSET\s+(\d+)\s*$", re.IGNORECASE)


def _json_safe(value: Any) -> Any:
	"""将数据库返回值转换为可持久化到会话状态的 JSON 数据。

	PostgreSQL 会将 timestamp 等列映射为 ``datetime``。审批恢复时查询结果会
	进入 LangGraph 检查点，因此必须在工具边界转换，避免恢复会话时序列化失败。
	"""
	return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _limit_rows(sql: str) -> str:
	"""给 SELECT/WITH 查询强制附加 LIMIT，避免模型查询返回整表数据。

	模型可能生成未带 LIMIT 的查询（或提示词被注入要求"完整返回"），这里在
	语句末尾统一追加 LIMIT。若原查询已带 LIMIT（含 OFFSET 形式），先剥离已
	存在的 LIMIT 子句并保留 OFFSET 语义后重加上限。
	"""
	normalized = sql.strip().rstrip(";").strip()
	offset_clause = ""
	match = _LIMIT_TAIL.search(normalized)
	if match:
		normalized = normalized[: match.start()].rstrip()
		if match.group(2):
			offset_clause = f" OFFSET {match.group(2)}"
	else:
		offset_match = _OFFSET_TAIL.search(normalized)
		if offset_match:
			normalized = normalized[: offset_match.start()].rstrip()
			offset_clause = f" OFFSET {offset_match.group(1)}"
	return f"{normalized} LIMIT {_MAX_QUERY_ROWS}{offset_clause}"


def database_schema(conn: Any) -> dict[str, list[str]]:
	"""读取全部业务表和字段。

	Args:
		conn: SQLite 连接。

	Returns:
		表名到字段名列表的映射。
	"""
	tables: dict[str, list[str]] = {}
	if isinstance(conn, sqlite3.Connection):
		rows = conn.execute(
			"SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
		).fetchall()
		for row in rows:
			table_name = str(row[0])
			if table_name not in _QUERYABLE_RELATIONS:
				continue
			escaped_name = table_name.replace('"', '""')
			columns = conn.execute(f'PRAGMA table_info("{escaped_name}")').fetchall()
			tables[table_name] = [str(column[1]) for column in columns]
		return tables
	rows = conn.execute(
		"""SELECT table_name AS name FROM information_schema.tables
		WHERE table_schema = 'public' ORDER BY table_name"""
	).fetchall()
	for row in rows:
		table_name = str(row["name"])
		if table_name not in _QUERYABLE_RELATIONS:
			continue
		columns = conn.execute(
			"""SELECT column_name FROM information_schema.columns
			WHERE table_schema = 'public' AND table_name = ? ORDER BY ordinal_position""",
			(table_name,),
		).fetchall()
		tables[table_name] = [str(column["column_name"]) for column in columns]
	return tables


def _extract_json_object(text: str) -> dict[str, Any] | None:
	"""从模型输出中提取第一个 JSON 对象。"""
	match = re.search(r"\{.*\}", text, re.DOTALL)
	if not match:
		return None
	try:
		value = json.loads(match.group(0))
	except (TypeError, ValueError):
		return None
	return value if isinstance(value, dict) else None


def _validate_readonly_sql(sql: str) -> str:
	"""校验模型 SQL 只能执行单条只读查询。"""
	normalized = sql.strip().rstrip(";").strip()
	if not normalized or not re.match(r"^(?:SELECT|WITH)\b", normalized, re.IGNORECASE):
		raise ValueError("只允许 SELECT 或 WITH 查询")
	if ";" in normalized or _FORBIDDEN_SQL.search(normalized):
		raise ValueError("SQL 包含不允许的操作")
	if re.search(r"\bWITH\s+RECURSIVE\b", normalized, re.IGNORECASE):
		# 递归 CTE 可构造无界计算，对交互式问数属于潜在 DoS 面，显式拒绝。
		raise ValueError("不支持 WITH RECURSIVE 递归查询")
	cte_names = {
		name.lower()
		for name in re.findall(r"(?:\bWITH\b|,)\s*([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(", normalized, re.IGNORECASE)
	}
	relations = {
		name.lower()
		for name in re.findall(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)", normalized, re.IGNORECASE)
	}
	unknown_relations = relations - _QUERYABLE_RELATIONS - cte_names
	if unknown_relations:
		raise ValueError(f"SQL 访问了未授权数据表：{', '.join(sorted(unknown_relations))}")
	return normalized


def validate_readonly_sql(sql: str) -> str:
	"""公开的只读 SQL 校验入口，供审批恢复阶段再次验证。"""
	return _limit_rows(_validate_readonly_sql(sql))


def _plan_query(
	question: str,
	schema: dict[str, list[str]],
	config: dict[str, Any],
	observations: list[str] | None = None,
	goal: str = "",
) -> tuple[str, list[Any]]:
	"""让模型结合前序执行观察，把用户问题转换成参数化 SQL。"""
	feedback = "\n".join(f"- {item}" for item in (observations or [])) or "无，这是第一次尝试。"
	goal_block = f"用户多轮合并后的目标（补充上文指代，与问题重复时以问题为准）：{goal}" if goal.strip() else "无"
	prompt = f"""你是 PostgreSQL 查询规划器。根据用户问题和数据库 schema 生成一条只读 SQL。

要求：
1. 只能使用 SELECT 或 WITH，不得修改数据库。
2. 必须使用 ? 占位符承载用户问题里的实体，参数按顺序放进 params。
3. 不要使用 LIMIT，完整返回所有符合条件的记录。
4. 用户询问“有哪些、多少、统计、排名、超过、低于”等条件时，准确转换成 WHERE、GROUP BY 或 ORDER BY。
5. registered_capital 是文本字段，可能包含“万人民币”“万美元”等单位。涉及注册资本数值比较时，保留原字段，并用 CAST、REPLACE 对明确单位的数据进行转换；不要把不同币种直接混算。
6. 用户提到“已发送、已回复、待发送、高分”等中文状态时，转换为 jobs.status、jobs.score 等实际字段。
7. 只输出 JSON，不要解释：{{"sql":"SELECT ... WHERE company LIKE ?","params":["%实体%"]}}
8. 如果提供了前序观察，先反思失败原因，再生成修正后的完整查询。不得重复已经失败的 SQL。
9. 涉及岗位评分、待评分数量或评分结果时，默认排除 title='HR主动沟通'、职位为空或 JD 为空的聊天占位记录；manual_ 记录在补齐真实职位和 JD 后可视为正式岗位。

数据库 schema：
{json.dumps(schema, ensure_ascii=False)}

用户问题：
{question}

用户多轮合并目标：
{goal_block}

前序观察：
{feedback}
"""
	answer = call_anthropic_text(prompt, config) or ""
	plan = _extract_json_object(answer)
	if not plan:
		raise ValueError("模型没有返回有效的 SQL 查询计划")
	sql = _validate_readonly_sql(str(plan.get("sql", "")))
	params = plan.get("params", [])
	if not isinstance(params, list):
		raise ValueError("SQL 参数必须是数组")
	return sql, params


def plan_database_query(
	conn: Any,
	question: str,
	config: dict[str, Any],
	*,
	job_id: str | None = None,
	goal: str = "",
) -> dict[str, Any]:
	"""只生成并校验查询计划，不访问业务数据。"""
	schema = database_schema(conn)
	if job_id:
		sql, params = "SELECT * FROM jobs WHERE id = ?", [job_id]
	else:
		sql, params = _plan_query(question, schema, config, goal=goal)
	return {
		"sql": _limit_rows(_validate_readonly_sql(sql)),
		"params": params,
		"tables": sorted(
			{
				name.lower()
				for name in re.findall(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)", sql, re.IGNORECASE)
			}
		),
		"row_limit": _MAX_QUERY_ROWS,
	}


def execute_database_query(conn: Any, sql: str, params: list[Any]) -> dict[str, Any]:
	"""再次校验并执行已获人工批准的只读 SQL。"""
	validated_sql = _limit_rows(_validate_readonly_sql(sql))
	if not isinstance(params, list):
		raise ValueError("SQL 参数必须是数组")
	rows = conn.execute(validated_sql, params).fetchall()
	return _json_safe({
		"schema": database_schema(conn),
		"query": {"sql": validated_sql, "params": params, "attempts": [{"attempt": 1, "status": "completed"}]},
		"rows": [dict(row) for row in rows],
		"row_count": len(rows),
	})


def query_database(
	database_path: Path,
	question: str,
	config: dict[str, Any],
	job_id: str | None = None,
	goal: str = "",
	conn: Any | None = None,
) -> dict[str, Any]:
	"""根据自然语言问题规划并执行只读 SQL。

	Args:
		database_path: BossHunter 业务数据库路径。
		question: 用户问题。
		config: AI 配置。
		job_id: 用户显式选择的岗位 ID。
		goal: 理解层合并多轮对话后的目标，用于消解"那上海呢"类指代。
		conn: 调用方已打开的数据库连接；传入时复用且不负责关闭。

	Returns:
		包含完整 schema、查询、参数和结果的上下文。
	"""
	owns_connection = conn is None
	# 未传入连接时按原逻辑开新连接：显式数据库文件路径保留给隔离测试，
	# 生产调用传入 Path() 占位走配置中的 PostgreSQL。
	if conn is None:
		conn = get_db(database_path) if str(database_path) not in {"", "."} else get_db()
	try:
		if not job_id and looks_like_semantic_question(question):
			try:
				payload = query_semantic_database(conn, question, config, goal)
				# 语义问数只暴露业务目录，不泄露助手消息、配置等物理表结构。
				payload["schema"] = semantic_catalog()
				return _json_safe(payload)
			except ValueError:
				# 无法映射到白名单语义对象时，保留现有只读 SQL 多轮修正作为兼容兜底。
				pass
		schema = database_schema(conn)
		attempts: list[dict[str, Any]] = []
		if job_id:
			sql, params = "SELECT * FROM jobs WHERE id = ?", [job_id]
			rows = conn.execute(sql, params).fetchall()
			attempts.append({"attempt": 1, "sql": sql, "params": params, "status": "completed"})
		else:
			observations: list[str] = []
			rows = []
			sql, params = "", []
			for attempt_number in range(1, _MAX_QUERY_ATTEMPTS + 1):
				try:
					sql, params = _plan_query(question, schema, config, observations, goal)
					sql = _limit_rows(sql)
					print(
						f"[AI助手] SQL 第 {attempt_number}/{_MAX_QUERY_ATTEMPTS} 次尝试：{sql}；参数={params}",
						flush=True,
					)
					rows = conn.execute(sql, params).fetchall()
					# 空结果重试一次即可：第二次仍为空视为真实没有符合条件的数据，
					# 不再消耗第三次模型调用；错误重试仍可用满三次。
					if rows or attempt_number >= 2:
						attempts.append(
							{"attempt": attempt_number, "sql": sql, "params": params, "status": "completed"}
						)
						break
					observation = "查询成功但返回 0 条记录，请检查字段、状态映射和过滤条件是否过严。"
					attempts.append(
						{
							"attempt": attempt_number,
							"sql": sql,
							"params": params,
							"status": "empty",
							"error": observation,
						}
					)
					observations.append(f"第 {attempt_number} 次：SQL={sql!r}，参数={params!r}。{observation}")
				except Exception as exc:
					observation = f"{type(exc).__name__}: {exc}"
					attempts.append(
						{
							"attempt": attempt_number,
							"sql": sql,
							"params": params,
							"status": "failed",
							"error": observation,
						}
					)
					observations.append(f"第 {attempt_number} 次失败：SQL={sql!r}，参数={params!r}，错误={observation}")
			else:
				rows = []

			if not sql or (attempts and attempts[-1]["status"] == "failed"):
				sql, params = "SELECT * FROM jobs ORDER BY updated_at DESC", []
				sql = _limit_rows(sql)
				print("[AI助手] SQL 多轮修正仍失败，使用只读安全兜底查询。", flush=True)
				rows = conn.execute(sql).fetchall()
				attempts.append({"attempt": len(attempts) + 1, "sql": sql, "params": params, "status": "fallback"})
		return _json_safe({
			"schema": schema,
			"query": {"sql": sql, "params": params, "attempts": attempts},
			"rows": [dict(row) for row in rows],
			"row_count": len(rows),
		})
	finally:
		if owns_connection:
			conn.close()


__all__ = [
	"database_schema",
	"execute_database_query",
	"plan_database_query",
	"query_database",
	"validate_readonly_sql",
]
