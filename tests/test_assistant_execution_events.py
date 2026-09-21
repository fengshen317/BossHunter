"""AI 助手执行轨迹状态归并测试。"""

from typing import Any

from bosshunter.assistant.execution_events import upsert_execution_event


def test_execution_event_keeps_only_latest_status_per_step() -> None:
	"""同一步骤完成后不应继续持久化旧的执行中状态。"""
	events: list[dict[str, Any]] = []
	upsert_execution_event(events, {"id": "context", "label": "读取上下文", "status": "running"})
	upsert_execution_event(events, {"id": "context", "label": "读取上下文", "status": "completed"})
	upsert_execution_event(events, {"id": "intent", "label": "识别意图", "status": "completed"})

	assert [event["id"] for event in events] == ["context", "intent"]
	assert events[0]["status"] == "completed"
	assert events[0]["phase"] == "understanding"
	assert events[0]["kind"] == "phase"
	assert events[0]["started_at"]
	assert events[0]["finished_at"]
	assert events[0]["duration_ms"] >= 0


def test_tool_event_has_retrieval_taxonomy_and_parent() -> None:
	"""工具事件应提供前端分组和缩进所需的分类信息。"""
	events: list[dict[str, Any]] = []

	upsert_execution_event(
		events,
		{"id": "tool-0-job_database", "label": "读取外部资料", "status": "running", "detail": "查询岗位"},
	)

	assert events[0]["kind"] == "tool"
	assert events[0]["phase"] == "retrieval"
	assert events[0]["parent_id"] == "react"
	assert events[0]["summary"] == "查询岗位"


def test_model_event_exposes_call_phase_and_parent() -> None:
	"""每次模型调用应提供可供前端实时展示的调用类型和所属阶段。"""
	events: list[dict[str, Any]] = []

	upsert_execution_event(
		events,
		{"id": "model-answer", "label": "调用模型：生成最终回答", "status": "running"},
	)

	assert events[0]["kind"] == "agent"
	assert events[0]["phase"] == "generation"
	assert events[0]["parent_id"] == "execution"
