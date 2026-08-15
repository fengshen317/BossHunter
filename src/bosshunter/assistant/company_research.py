"""公司背调子 Agent。"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from bosshunter.ai.credentials import call_anthropic_text
from bosshunter.assistant.web_search import search_web


def _research_topic(company: str, topic: str) -> dict[str, Any]:
	"""搜索一个背调主题并保留来源信息。"""
	results = search_web(f"{company} {topic}", limit=4)
	return {"topic": topic, "results": results}


def research_company(company: str, config: dict[str, Any]) -> dict[str, Any]:
	"""并行执行公司背调，再由子 Agent 汇总结果。

	Args:
		company: 公司名称。
		config: AI 配置。

	Returns:
		包含公司名称、研究结果和总结的字典。
	"""
	topics = ["公司官网和主营业务", "融资与经营状况", "员工评价与工作氛围", "近期新闻与风险"]
	with ThreadPoolExecutor(max_workers=len(topics), thread_name_prefix="company-research") as pool:
		parts = list(pool.map(lambda topic: _research_topic(company, topic), topics))

	prompt = f"""你是公司的背调子 Agent。只根据下面的搜索结果总结，不要编造事实。
公司：{company}
搜索结果：{json.dumps(parts, ensure_ascii=False)}
请用中文输出：公司概况、业务与产品、经营/融资、员工体验、风险点、面试建议。每个事实尽量标注来源 URL。"""
	summary = call_anthropic_text(prompt, config, max_tokens=1800) or "未能生成背调总结，请查看原始搜索结果。"
	return {"company": company, "summary": summary, "topics": parts}


__all__ = ["research_company"]
