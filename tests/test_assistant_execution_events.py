"""AI 助手执行轨迹状态归并测试。"""

from bosshunter.assistant.execution_events import upsert_execution_event


def test_execution_event_keeps_only_latest_status_per_step() -> None:
	"""同一步骤完成后不应继续持久化旧的执行中状态。"""
	events: list[dict[str, str]] = []
	upsert_execution_event(events, {"id": "context", "label": "读取上下文", "status": "running"})
	upsert_execution_event(events, {"id": "context", "label": "读取上下文", "status": "completed"})
	upsert_execution_event(events, {"id": "intent", "label": "识别意图", "status": "completed"})

	assert events == [
		{"id": "context", "label": "读取上下文", "status": "completed"},
		{"id": "intent", "label": "识别意图", "status": "completed"},
	]
