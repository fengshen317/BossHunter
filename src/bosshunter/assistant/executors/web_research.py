"""只研究入围对象的公开信息执行器。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def execute_web_research(
	question: str,
	dependency_results: dict[str, Any],
	search: Callable[[str], Any],
	max_results: int = 5,
) -> dict[str, Any]:
	"""根据上游筛选结果构造有界联网查询。

	Args:
		question: 用户原始研究问题。
		dependency_results: 上游私有资料匹配结果。
		search: 基础联网检索回调。
		max_results: 最多研究对象数。

	Returns:
		查询、研究对象和公开来源。
	"""
	companies = _selected_companies(dependency_results)[:max_results]
	if companies:
		query = f"{question}；仅研究以下入围公司：{'、'.join(companies)}"
	else:
		query = question
	result = search(query)
	sources = result.get("sources", []) if isinstance(result, dict) else result
	return {
		"query": query,
		"researched_companies": companies,
		"sources": list(sources or [])[:12],
	}


def _selected_companies(dependency_results: dict[str, Any]) -> list[str]:
	"""从直接依赖结果中提取入围公司。"""
	companies: list[str] = []
	for result in dependency_results.values():
		if not isinstance(result, dict):
			continue
		for company in result.get("selected_companies", []):
			name = str(company).strip()
			if name:
				companies.append(name)
	return list(dict.fromkeys(companies))


__all__ = ["execute_web_research"]
