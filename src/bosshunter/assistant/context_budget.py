"""Token 优先的会话上下文预算与完整轮次选择。"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any


_CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_MESSAGE_OVERHEAD_TOKENS = 12


@dataclass(frozen=True, slots=True)
class ContextBudgetSettings:
	"""上下文预算配置。

	Attributes:
		context_window_tokens: 未知模型使用的保守上下文窗口。
		output_reserve_tokens: 为模型输出预留的 Token。
		safety_margin_ratio: 为估算误差保留的比例。
		recent_history_ratio: 可用输入中分配给近期历史的比例。
		recent_turn_min: 尽力保留的最少完整轮次。
		recent_turn_max: 即使 Token 足够也不超过的轮次数。
		summary_max_tokens: 持久滚动摘要的最大 Token。
		pending_digest_max_tokens: 未达到压缩批次时本地摘录的最大 Token。
		compaction_batch_tokens: 触发模型摘要的旧消息 Token 数。
		compaction_batch_turns: Token 较少时触发摘要的辅助轮数。
	"""

	context_window_tokens: int = 32_768
	output_reserve_tokens: int = 4_096
	safety_margin_ratio: float = 0.15
	recent_history_ratio: float = 0.35
	recent_turn_min: int = 2
	recent_turn_max: int = 12
	summary_max_tokens: int = 2_048
	pending_digest_max_tokens: int = 1_024
	compaction_batch_tokens: int = 8_192
	compaction_batch_turns: int = 8
	memory_max_items: int = 8
	memory_max_tokens: int = 2_048

	@classmethod
	def from_config(cls, config: dict[str, Any]) -> "ContextBudgetSettings":
		"""从配置读取上下文设置，并将异常值限制在安全范围内。"""
		context = config.get("assistant", {}).get("context", {})
		memory = config.get("assistant", {}).get("memory", {})
		return cls(
			context_window_tokens=_bounded_int(context.get("default_context_window_tokens"), 32_768, 8_192, 2_000_000),
			output_reserve_tokens=_bounded_int(context.get("output_reserve_tokens"), 4_096, 512, 262_144),
			safety_margin_ratio=_bounded_float(context.get("safety_margin_ratio"), 0.15, 0.05, 0.40),
			recent_history_ratio=_bounded_float(context.get("recent_history_ratio"), 0.35, 0.10, 0.70),
			recent_turn_min=_bounded_int(context.get("recent_turn_min"), 2, 1, 10),
			recent_turn_max=_bounded_int(context.get("recent_turn_max"), 12, 2, 50),
			summary_max_tokens=_bounded_int(context.get("summary_max_tokens"), 2_048, 256, 16_384),
			pending_digest_max_tokens=_bounded_int(context.get("pending_digest_max_tokens"), 1_024, 128, 8_192),
			compaction_batch_tokens=_bounded_int(context.get("compaction_batch_tokens"), 8_192, 512, 131_072),
			compaction_batch_turns=_bounded_int(context.get("compaction_batch_turns"), 8, 2, 50),
			memory_max_items=_bounded_int(memory.get("retrieval_max_items"), 8, 1, 50),
			memory_max_tokens=_bounded_int(memory.get("retrieval_max_tokens"), 2_048, 128, 16_384),
		)

	def input_budget(self) -> int:
		"""返回扣除输出预留和安全余量后的输入预算。"""
		margin = math.ceil(self.context_window_tokens * self.safety_margin_ratio)
		return max(1_024, self.context_window_tokens - self.output_reserve_tokens - margin)

	def recent_history_budget(self, mandatory_tokens: int = 0) -> int:
		"""根据剩余输入计算近期历史预算。"""
		remaining = max(0, self.input_budget() - max(0, mandatory_tokens))
		return max(0, math.floor(remaining * self.recent_history_ratio))


@dataclass(frozen=True, slots=True)
class HistorySelection:
	"""Token 预算选择后的近期消息与被淘汰轮次。"""

	messages: list[dict[str, Any]]
	omitted_turns: list[list[dict[str, Any]]]
	estimated_tokens: int
	turn_count: int


def estimate_text_tokens(text: str) -> int:
	"""保守估算中英文混合文本的 Token 数。

	中文字符按一个 Token 计算；其余字符按每四个字符一个 Token
	估算，并为标点和分词差异增加少量固定余量。
	"""
	value = str(text or "")
	if not value:
		return 0
	cjk_count = len(_CJK_PATTERN.findall(value))
	non_cjk_count = len(value) - cjk_count
	return cjk_count + math.ceil(non_cjk_count / 4) + 2


def estimate_message_tokens(message: dict[str, Any]) -> int:
	"""估算单条消息正文、角色和结构化元数据的 Token 数。"""
	content = str(message.get("content", ""))
	role = str(message.get("role", ""))
	message_type = str(message.get("message_type", ""))
	return estimate_text_tokens(content) + estimate_text_tokens(role + message_type) + _MESSAGE_OVERHEAD_TOKENS


def split_complete_turns(messages: list[dict[str, Any]]) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
	"""把消息拆成完整轮次和末尾尚未回答的用户消息。"""
	turns: list[list[dict[str, Any]]] = []
	for message in messages:
		if message.get("role") == "user":
			turns.append([message])
		elif turns:
			turns[-1].append(message)
		else:
			# 历史孤立助手消息不构成可保留轮次，避免与后续用户消息错误配对。
			continue
	if turns and len(turns[-1]) == 1 and turns[-1][0].get("role") == "user":
		return turns[:-1], turns[-1]
	return turns, []


def select_recent_turns(
	messages: list[dict[str, Any]],
	*,
	token_budget: int,
	min_turns: int,
	max_turns: int,
) -> HistorySelection:
	"""按 Token 优先、轮数为辅选择最新完整轮次。"""
	turns, pending = split_complete_turns(messages)
	selected: list[list[dict[str, Any]]] = []
	used_tokens = sum(estimate_message_tokens(message) for message in pending)
	max_count = max(1, max_turns)
	minimum = max(0, min(min_turns, max_count))
	for turn in reversed(turns):
		turn_tokens = sum(estimate_message_tokens(message) for message in turn)
		fits = used_tokens + turn_tokens <= max(0, token_budget)
		if len(selected) >= max_count or (not fits and len(selected) >= minimum):
			break
		selected.append(turn)
		used_tokens += turn_tokens
	selected.reverse()
	omitted_count = len(turns) - len(selected)
	return HistorySelection(
		messages=[message for turn in selected for message in turn] + pending,
		omitted_turns=turns[:omitted_count],
		estimated_tokens=used_tokens,
		turn_count=len(selected),
	)


def should_compact(backlog_turns: list[list[dict[str, Any]]], settings: ContextBudgetSettings) -> bool:
	"""判断待压缩旧轮次是否达到批量摘要阈值。"""
	if len(backlog_turns) >= settings.compaction_batch_turns:
		return True
	token_count = sum(estimate_message_tokens(message) for turn in backlog_turns for message in turn)
	return token_count >= settings.compaction_batch_tokens


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
	"""把任意输入转换为位于边界内的整数。"""
	try:
		parsed = int(value)
	except (TypeError, ValueError):
		parsed = default
	return max(minimum, min(parsed, maximum))


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
	"""把任意输入转换为位于边界内的浮点数。"""
	try:
		parsed = float(value)
	except (TypeError, ValueError):
		parsed = default
	return max(minimum, min(parsed, maximum))


__all__ = [
	"ContextBudgetSettings",
	"HistorySelection",
	"estimate_message_tokens",
	"estimate_text_tokens",
	"select_recent_turns",
	"should_compact",
	"split_complete_turns",
]
