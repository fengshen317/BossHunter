"""最终回答上下文压缩回归测试。"""

import json

from bosshunter.assistant.answer_context import compact_answer_inputs, compact_job_context


def test_compact_job_context_keeps_total_and_limits_rows() -> None:
	"""岗位明细过多时应保留真实总数，但限制进入最终模型的行数。"""
	raw = json.dumps({"row_count": 80, "rows": [{"id": str(index)} for index in range(80)], "schema": {"secret": []}})

	result = json.loads(compact_job_context(raw))

	assert result["row_count"] == 80
	assert len(result["rows"]) == 50
	assert result["truncated"] is True
	assert "schema" not in result


def test_compact_job_context_preserves_query_pagination_metadata() -> None:
	"""回答上下文必须区分真实总数、查询返回数和最终可见数。"""
	raw = json.dumps({
		"row_count": 228,
		"total_count": 228,
		"returned_count": 20,
		"limit": 20,
		"has_more": True,
		"rows": [{"id": str(index)} for index in range(20)],
	})

	result = json.loads(compact_job_context(raw))

	assert result["row_count"] == 228
	assert result["total_count"] == 228
	assert result["returned_count"] == 20
	assert result["visible_row_count"] == 20
	assert result["has_more"] is True
	assert result["truncated"] is True


def test_compact_answer_inputs_removes_duplicate_database_result() -> None:
	"""岗位数据库结果已经单独提供，不能再次塞进工具结果扩大 Prompt。"""
	result = compact_answer_inputs(
		history=[{"role": "user", "content": "问题"}] * 12,
		resume="简" * 20_000,
		memories=[],
		web_results=[],
		tool_results=[
			{"name": "job_database", "result": {"rows": [1, 2]}},
			{"name": "web_search", "result": {"count": 2}},
		],
	)

	assert len(result["history"]) == 8
	assert len(result["resume"]) == 12_000
	assert [item["name"] for item in result["tool_results"]] == ["web_search"]
