"""结构化业务问数执行器。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def execute_analytics(
	question: str,
	query: Callable[[str], Any],
) -> Any:
	"""通过受限查询回调执行问数，不跨域降级。

	Args:
		question: 本步骤需要回答的结构化问题。
		query: 已封装只读校验、连接和行数限制的查询函数。

	Returns:
		查询工具的原始结果，随后统一转换为 EvidenceResult。
	"""
	result = query(question)
	if result is None:
		raise ValueError("问数执行器未获得结构化结果")
	return result


__all__ = ["execute_analytics"]
