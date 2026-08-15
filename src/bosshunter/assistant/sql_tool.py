"""岗位数据库的自然语言只读 SQL 查询工具。"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from bosshunter.ai.credentials import call_anthropic_text


_FORBIDDEN_SQL = re.compile(
	r"\b(?:INSERT|UPDATE|DELETE|REPLACE|DROP|ALTER|CREATE|ATTACH|DETACH|PRAGMA|VACUUM|REINDEX|ANALYZE)\b",
	re.IGNORECASE,
)
_MAX_QUERY_ATTEMPTS = 3


def database_schema(conn: sqlite3.Connection) -> dict[str, list[str]]:
	"""读取全部业务表和字段。

	Args:
		conn: SQLite 连接。

	Returns:
		表名到字段名列表的映射。
	"""
	tables: dict[str, list[str]] = {}
	rows = conn.execute(
		"SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
	).fetchall()
	for row in rows:
		table_name = str(row[0])
		escaped_name = table_name.replace('"', '""')
		columns = conn.execute(f'PRAGMA table_info("{escaped_name}")').fetchall()
		tables[table_name] = [str(column[1]) for column in columns]
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
	return normalized


def _plan_query(
	question: str,
	schema: dict[str, list[str]],
	config: dict[str, Any],
	observations: list[str] | None = None,
) -> tuple[str, list[Any]]:
	"""让模型结合前序执行观察，把用户问题转换成参数化 SQL。"""
	feedback = "\n".join(f"- {item}" for item in (observations or [])) or "无，这是第一次尝试。"
	prompt = f"""你是 SQLite 查询规划器。根据用户问题和数据库 schema 生成一条只读 SQL。

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

前序观察：
{feedback}
"""
	answer = call_anthropic_text(prompt, config, max_tokens=1200) or ""
	plan = _extract_json_object(answer)
	if not plan:
		raise ValueError("模型没有返回有效的 SQL 查询计划")
	sql = _validate_readonly_sql(str(plan.get("sql", "")))
	params = plan.get("params", [])
	if not isinstance(params, list):
		raise ValueError("SQL 参数必须是数组")
	return sql, params


def query_database(
	database_path: Path,
	question: str,
	config: dict[str, Any],
	job_id: str | None = None,
) -> dict[str, Any]:
	"""根据自然语言问题规划并执行只读 SQL。

	Args:
		database_path: BossHunter 业务数据库路径。
		question: 用户问题。
		config: AI 配置。
		job_id: 用户显式选择的岗位 ID。

	Returns:
		包含完整 schema、查询、参数和结果的上下文。
	"""
	uri = database_path.resolve().as_uri().replace("file:///", "file:///") + "?mode=ro"
	conn = sqlite3.connect(uri, uri=True)
	conn.row_factory = sqlite3.Row
	try:
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
					sql, params = _plan_query(question, schema, config, observations)
					print(
						f"[AI助手] SQL 第 {attempt_number}/{_MAX_QUERY_ATTEMPTS} 次尝试：{sql}；参数={params}",
						flush=True,
					)
					rows = conn.execute(sql, params).fetchall()
					if rows or attempt_number == _MAX_QUERY_ATTEMPTS:
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
				except (ValueError, sqlite3.Error) as exc:
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
				print("[AI助手] SQL 多轮修正仍失败，使用只读安全兜底查询。", flush=True)
				rows = conn.execute(sql).fetchall()
				attempts.append({"attempt": len(attempts) + 1, "sql": sql, "params": params, "status": "fallback"})
		return {
			"schema": schema,
			"query": {"sql": sql, "params": params, "attempts": attempts},
			"rows": [dict(row) for row in rows],
			"row_count": len(rows),
		}
	finally:
		conn.close()


__all__ = ["database_schema", "query_database"]
