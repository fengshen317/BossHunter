"""AI 助手执行轨迹状态工具。"""

from datetime import datetime, timezone
from typing import Any


_TERMINAL_STATUSES = {"completed", "failed", "stopped", "skipped"}


def _now() -> str:
	"""返回可供前后端稳定解析的 UTC 时间。"""
	return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _event_taxonomy(event_id: str) -> tuple[str, str, str | None]:
	"""从现有稳定事件 ID 推导展示分类，兼容尚未迁移的图节点。"""
	if event_id == "context":
		return "phase", "understanding", None
	if event_id in {"intent", "ambiguity", "clarification"}:
		return "phase", "understanding", "context"
	if event_id in {"planner", "plan_evaluation", "replanner", "capability"} or event_id.startswith("plan-"):
		return "plan", "planning", None
	if event_id == "react":
		return "agent", "retrieval", None
	if event_id.startswith("model-"):
		if "goal" in event_id or "tool-planner" in event_id:
			return "agent", "understanding" if "goal" in event_id else "planning", None
		if "react" in event_id:
			return "agent", "retrieval", "react"
		return "agent", "generation", "execution"
	if event_id.startswith("tool-") or event_id.startswith("read-tool-"):
		parent_id = "react" if event_id.startswith("tool-") else None
		kind = "agent" if any(part in event_id for part in ("-agent", "-react-action", "-react-observation")) else "tool"
		return kind, "retrieval", parent_id
	if event_id.startswith("dag-"):
		return "tool", "execution", "planner"
	if event_id == "approval":
		return "approval", "approval", None
	if event_id == "execution":
		return "phase", "generation", None
	if event_id == "result":
		return "result", "finalization", None
	return "phase", "execution", None


def normalize_execution_event(
	event: dict[str, Any],
	previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
	"""补齐统一执行记录需要的分类、层级和时间字段。

	Args:
		event: 图节点发出的原始事件。
		previous: 同一事件 ID 的上一状态，用于保留开始时间并计算耗时。

	Returns:
		兼容旧字段且可直接通过 SSE 展示的 V2 事件。
	"""
	event_id = str(event.get("id", "")).strip()
	kind, phase, inferred_parent = _event_taxonomy(event_id)
	now = _now()
	status = str(event.get("status", "running"))
	started_at = str((previous or {}).get("started_at") or event.get("started_at") or now)
	finished_at = str(event.get("finished_at") or now) if status in _TERMINAL_STATUSES else None
	duration_ms: int | None = None
	if finished_at:
		try:
			duration_ms = max(
				0,
				int((datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)).total_seconds() * 1000),
			)
		except ValueError:
			duration_ms = None

	normalized = {
		"version": 2,
		"id": event_id,
		"parent_id": event.get("parent_id", inferred_parent),
		"kind": event.get("kind", kind),
		"phase": event.get("phase", phase),
		"label": str(event.get("label", event_id)),
		"status": status,
		"summary": str(event.get("summary") or event.get("detail") or ""),
		"detail": str(event.get("detail") or event.get("summary") or ""),
		"started_at": started_at,
		"finished_at": finished_at,
		"duration_ms": duration_ms,
	}
	return {key: value for key, value in normalized.items() if value is not None}


def upsert_execution_event(events: list[dict[str, Any]], event: dict[str, Any]) -> dict[str, Any]:
	"""按步骤 ID 原位更新执行状态，保留步骤首次出现的展示顺序。"""
	existing_index = next(
		(index for index, current in enumerate(events) if current.get("id") == event.get("id")),
		None,
	)
	previous = events[existing_index] if existing_index is not None else None
	normalized = normalize_execution_event(event, previous)
	if existing_index is None:
		events.append(normalized)
	else:
		events[existing_index] = normalized
	return normalized
