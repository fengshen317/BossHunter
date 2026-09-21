"""Token 优先上下文预算的回归测试。"""

from bosshunter.assistant.context_budget import (
	ContextBudgetSettings,
	estimate_text_tokens,
	select_recent_turns,
	should_compact,
)


def _turn(index: int, size: int = 40) -> list[dict[str, str]]:
	"""创建一个完整测试轮次。"""
	return [
		{"role": "user", "content": f"问题 {index} " + "中" * size},
		{"role": "assistant", "content": f"回答 {index} " + "答" * size},
	]


def test_short_history_is_kept_when_it_fits_token_budget() -> None:
	"""短历史未超 Token 预算时不应仅因超过八轮而压缩。"""
	messages = [message for index in range(10) for message in _turn(index, 10)]

	selection = select_recent_turns(messages, token_budget=4_000, min_turns=2, max_turns=12)

	assert selection.turn_count == 10
	assert selection.omitted_turns == []


def test_long_history_is_trimmed_by_tokens_before_turn_limit() -> None:
	"""即使轮数很少，超出 Token 预算时也应淘汰最早完整轮次。"""
	messages = [message for index in range(3) for message in _turn(index, 1_500)]

	selection = select_recent_turns(messages, token_budget=3_200, min_turns=1, max_turns=12)

	assert 1 <= selection.turn_count < 3
	assert selection.omitted_turns
	assert selection.messages[0]["role"] == "user"


def test_turn_cap_limits_many_tiny_turns() -> None:
	"""大量短轮次即使未超预算，也必须受辅助轮数上限限制。"""
	messages = [message for index in range(30) for message in _turn(index, 1)]

	selection = select_recent_turns(messages, token_budget=20_000, min_turns=2, max_turns=12)

	assert selection.turn_count == 12
	assert len(selection.omitted_turns) == 18


def test_compaction_is_batched_by_tokens_with_turns_as_fallback() -> None:
	"""摘要仅在旧消息形成足够大的批次时触发。"""
	settings = ContextBudgetSettings()
	small_backlog = [_turn(index, 10) for index in range(7)]
	turn_backlog = [_turn(index, 10) for index in range(8)]
	token_backlog = [_turn(1, 5_000)]

	assert should_compact(small_backlog, settings) is False
	assert should_compact(turn_backlog, settings) is True
	assert should_compact(token_backlog, settings) is True


def test_token_estimator_is_conservative_for_chinese_and_protocol_overhead() -> None:
	"""中文和消息结构必须计入估算，避免预算只统计英文字符。"""
	assert estimate_text_tokens("这是中文上下文") >= 7
	selection = select_recent_turns(_turn(1, 10), token_budget=100, min_turns=1, max_turns=12)
	assert selection.estimated_tokens > estimate_text_tokens("问题 1 " + "中" * 10)
