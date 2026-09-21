"""问数 SQL 审批边界的回归测试。"""

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from bosshunter.assistant.sql_tool import _json_safe, execute_database_query, plan_database_query, validate_readonly_sql
from bosshunter.db import get_db


def test_plan_database_query_does_not_execute_generated_sql(tmp_path: Path) -> None:
	"""计划阶段只能生成 SQL，不能提前读取查询结果。"""
	conn = get_db(tmp_path / "approval.db")
	plan = json.dumps({"sql": "SELECT id, title FROM jobs WHERE score = ?", "params": [0]})
	try:
		with patch("bosshunter.assistant.sql_tool.call_anthropic_text", return_value=plan):
			result = plan_database_query(conn, "哪些岗位未评分", {})
	finally:
		conn.close()

	assert result["sql"].endswith("LIMIT 200")
	assert result["params"] == [0]
	assert result["tables"] == ["jobs"]


def test_execute_database_query_revalidates_sql(tmp_path: Path) -> None:
	"""审批恢复后仍须重新校验，只读查询可以执行，写操作必须拒绝。"""
	conn = get_db(tmp_path / "approval.db")
	try:
		payload = execute_database_query(conn, "SELECT id FROM jobs", [])
		with pytest.raises(ValueError, match="只允许"):
			execute_database_query(conn, "DELETE FROM jobs", [])
	finally:
		conn.close()

	assert payload["row_count"] == 0
	assert payload["query"]["sql"].endswith("LIMIT 200")


def test_validate_readonly_sql_rejects_multiple_statements() -> None:
	"""修改后的 SQL 不能借助第二条语句绕过审批。"""
	with pytest.raises(ValueError, match="不允许"):
		validate_readonly_sql("SELECT id FROM jobs; DROP TABLE jobs")


def test_query_result_is_json_safe_for_checkpoint_persistence() -> None:
	"""PostgreSQL 的 datetime 查询列应在进入会话状态前转为字符串。"""
	value = _json_safe({"created_at": datetime(2026, 9, 10, 8, 30, 0)})

	assert value == {"created_at": "2026-09-10 08:30:00"}
