"""AI 助手执行轨迹状态工具。"""


def upsert_execution_event(events: list[dict[str, str]], event: dict[str, str]) -> None:
	"""按步骤 ID 原位更新执行状态，保留步骤首次出现的展示顺序。"""
	existing_index = next(
		(index for index, current in enumerate(events) if current.get("id") == event.get("id")),
		None,
	)
	if existing_index is None:
		events.append(event)
	else:
		events[existing_index] = event
