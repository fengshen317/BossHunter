"""受控 ReAct 循环的动作过滤和停止条件。"""

import json
from typing import Any

from bosshunter.assistant.models import CapabilityMatch, ReactAction, ReactObservation


MAX_REACT_ITERATIONS = 4


def action_signature(tool_name: str, arguments: dict[str, Any]) -> str:
	"""生成稳定的动作签名，用于阻止重复工具调用。

	Args:
		tool_name: 工具名称。
		arguments: 工具参数。

	Returns:
		可用于集合比较的稳定字符串。
	"""
	return f"{tool_name}:{json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)}"


def allowed_read_tools(matches: list[CapabilityMatch]) -> set[str]:
	"""从能力匹配结果中提取允许进入 ReAct 的只读工具。

	Args:
		matches: 能力匹配结果。

	Returns:
		只读且无需审批的工具名称集合。
	"""
	return {
		str(match["tool_name"])
		for match in matches
		if match["read_only"] and not match["requires_approval"] and match["tool_name"]
	}


def initial_react_action(
	matches: list[CapabilityMatch], observations: list[ReactObservation]
) -> ReactAction | None:
	"""从能力匹配中选择第一个尚未执行的只读动作。

	Args:
		matches: 能力匹配结果。
		observations: 已完成或失败的观察。

	Returns:
		下一项只读动作；没有可执行动作时返回 None。
	"""
	observed = {action_signature(item["tool_name"], item["arguments"]) for item in observations}
	for match in matches:
		tool_name = match["tool_name"]
		if not tool_name or not match["read_only"] or match["requires_approval"]:
			continue
		if action_signature(tool_name, match["arguments"]) in observed:
			continue
		return {"tool_name": tool_name, "arguments": match["arguments"], "reason": "执行已匹配的只读能力。"}
	return None


def select_model_action(
	tool_calls: list[dict[str, Any]],
	matches: list[CapabilityMatch],
	observations: list[ReactObservation],
) -> ReactAction | None:
	"""从模型动作中选择首个允许且未重复的只读调用。

	Args:
		tool_calls: 模型返回的标准工具调用。
		matches: 能力匹配结果，用于限定权限。
		observations: 已执行动作，用于阻止重复循环。

	Returns:
		通过权限检查的动作；没有合法动作时返回 None。
	"""
	allowed = allowed_read_tools(matches)
	observed = {action_signature(item["tool_name"], item["arguments"]) for item in observations}
	for call in tool_calls:
		name = str(call.get("name", ""))
		arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
		if name not in allowed or action_signature(name, arguments) in observed:
			continue
		return {"tool_name": name, "arguments": arguments, "reason": "模型根据最新观察选择了只读工具。"}
	return None


def can_continue(iteration: int, action: ReactAction | None) -> bool:
	"""判断 ReAct 循环是否仍在有界范围内。

	Args:
		iteration: 已完成的工具调用次数。
		action: 下一项动作。

	Returns:
		存在合法动作且未超过最大轮次时返回 True。
	"""
	return action is not None and iteration < MAX_REACT_ITERATIONS


__all__ = [
	"MAX_REACT_ITERATIONS",
	"action_signature",
	"allowed_read_tools",
	"can_continue",
	"initial_react_action",
	"select_model_action",
]
