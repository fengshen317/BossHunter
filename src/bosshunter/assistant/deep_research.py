"""Deep Agents 只读研究层。

该模块只负责公开资料检索和总结，不暴露任何写库、发送或后台任务工具。
敏感动作仍由外层 LangGraph 审批流程统一控制。
"""

from __future__ import annotations

import json
import asyncio
import os
import re
from collections.abc import Callable
from threading import Lock
from typing import Any

from bosshunter.ai.credentials import build_anthropic_client_kwargs, resolve_anthropic_model
from bosshunter.assistant.company_research import research_company
from bosshunter.assistant.web_search import search_web


ResearchEventCallback = Callable[[str, str, str], None]


def _build_model(config: dict[str, Any]) -> Any:
	"""根据 BossHunter 现有 AI 配置创建 LangChain 模型适配器。"""
	ai_cfg = config.get("ai", {}) if isinstance(config, dict) else {}
	provider = ai_cfg.get("provider", "anthropic")
	model_name = str(ai_cfg.get("model") or "claude-sonnet-4-6")
	if provider == "openai_compatible":
		from langchain_openai import ChatOpenAI

		api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or ai_cfg.get("api_key")
		base_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL") or ai_cfg.get("base_url")
		if not api_key or not base_url:
			raise ValueError("Deep Agents 缺少 OpenAI 兼容接口的 api_key 或 base_url")
		return ChatOpenAI(model=model_name, api_key=api_key, base_url=base_url, temperature=0.1)

	from langchain_anthropic import ChatAnthropic

	client_kwargs = build_anthropic_client_kwargs(config)
	if not client_kwargs.get("api_key") and not client_kwargs.get("auth_token"):
		raise ValueError("Deep Agents 缺少 Anthropic API 凭据")
	return ChatAnthropic(
		model_name=resolve_anthropic_model(model_name, config),
		max_tokens=int(ai_cfg.get("anthropic_max_tokens") or 8192),
		**client_kwargs,
	)


def _message_text(message: Any) -> str:
	"""从 LangChain 消息的字符串或内容块中提取正文。"""
	content = getattr(message, "content", "")
	if isinstance(content, str):
		return content.strip()
	if not isinstance(content, list):
		return str(content).strip()
	parts: list[str] = []
	for block in content:
		if isinstance(block, dict) and isinstance(block.get("text"), str):
			parts.append(block["text"])
	return "\n".join(parts).strip()


def _run_company_agent(company: str, config: dict[str, Any]) -> dict[str, Any]:
	"""运行一个仅拥有公开网页搜索权限的公司研究 Deep Agent。"""
	from deepagents import create_deep_agent
	from langchain_core.tools import tool

	sources: list[dict[str, str]] = []
	sources_lock = Lock()

	@tool
	def search_public_web(query: str) -> str:
		"""搜索公开网页，返回标题、URL 和摘要；仅用于只读研究。"""
		results = search_web(query, limit=5)
		with sources_lock:
			for item in results:
				if item.get("url") and not any(saved.get("url") == item["url"] for saved in sources):
					sources.append(item)
		return json.dumps(results, ensure_ascii=False, default=str)

	agent = create_deep_agent(
		model=_build_model(config),
		tools=[search_public_web],
		system_prompt=f"""你是 BossHunter 的公司研究 Agent，只研究“{company}”。
你只能调用只读的公开网页搜索工具，不得执行写库、投递、发送或后台任务。
应从官网与主营业务、经营融资、员工体验、近期新闻和风险五个角度检索；优先采用公司官网等一手来源。
资料不足时明确说明，不得编造。最终用简洁中文给出结构化总结，并在事实后保留来源 URL。""",
		name=f"company-research-{re.sub(r'[^a-zA-Z0-9]+', '-', company).strip('-') or 'worker'}",
	)
	result = agent.invoke({"messages": [{"role": "user", "content": f"研究 {company}，给出适合求职决策的公司报告。"}]})
	messages = result.get("messages", []) if isinstance(result, dict) else []
	summary = next((_message_text(message) for message in reversed(messages) if _message_text(message)), "")
	if not summary:
		raise RuntimeError(f"{company} 的 Deep Agent 未返回研究结论")
	return {
		"company": company,
		"summary": summary,
		"topics": [{"topic": "Deep Agent 公开资料研究", "results": sources}],
		"engine": "deepagents",
	}


def research_question(question: str, config: dict[str, Any]) -> dict[str, Any]:
	"""使用只读 Deep Agent 研究开放式问题并保留检索来源。

	Args:
		question: 用户当前问题。
		config: BossHunter AI 配置。

	Returns:
		包含总结、来源和执行引擎的研究结果。
	"""
	from deepagents import create_deep_agent
	from langchain_core.tools import tool

	sources: list[dict[str, str]] = []
	sources_lock = Lock()

	@tool
	def search_public_web(query: str) -> str:
		"""搜索公开网页并返回可引用的标题、URL 与摘要。"""
		results = search_web(query, limit=5)
		with sources_lock:
			for item in results:
				if item.get("url") and not any(saved.get("url") == item["url"] for saved in sources):
					sources.append(item)
		return json.dumps(results, ensure_ascii=False, default=str)

	agent = create_deep_agent(
		model=_build_model(config),
		tools=[search_public_web],
		system_prompt="""你是 BossHunter 的通用研究 Agent，只能使用公开网页搜索这一只读工具。
根据问题自主规划检索词，优先一手来源；交叉核对关键事实，资料不足时明确说明。
不得执行写库、投递、发送或后台任务。最终用简洁中文总结，并为事实保留来源 URL。""",
		name="general-research-worker",
	)
	result = agent.invoke({"messages": [{"role": "user", "content": question}]})
	messages = result.get("messages", []) if isinstance(result, dict) else []
	summary = next((_message_text(message) for message in reversed(messages) if _message_text(message)), "")
	if not summary:
		raise RuntimeError("通用研究 Deep Agent 未返回结论")
	return {"summary": summary, "sources": sources, "engine": "deepagents"}


async def research_companies_async(
	companies: list[str],
	config: dict[str, Any],
	on_event: ResearchEventCallback | None = None,
	max_workers: int = 4,
) -> list[dict[str, Any]]:
	"""异步并行运行多个公司研究 Deep Agent，并实时发送 ReAct 摘要事件。

	Args:
		companies: 已去重的公司名称。
		config: BossHunter AI 配置。
		on_event: 单个工作者状态回调，参数为公司、状态和详情。
		max_workers: 最大并发工作者数量。

	Returns:
		按输入公司顺序排列的研究结果。
	"""
	company_names = list(dict.fromkeys(company.strip() for company in companies if company.strip()))
	if not company_names:
		return []
	for company in company_names:
		if on_event:
			on_event(company, "running", "Deep Agent 已接收独立研究任务")

	semaphore = asyncio.Semaphore(max(1, max_workers))

	async def run_one(company: str) -> dict[str, Any]:
		"""在工作线程中运行同步 SDK，避免阻塞协调事件循环。"""
		async with semaphore:
			if on_event:
				on_event(company, "running", "ReAct Action：分析研究目标并选择公开搜索工具")
			try:
				result = await asyncio.to_thread(_run_company_agent, company, config)
			except Exception as exc:
				result = await asyncio.to_thread(research_company, company, config)
				result["engine"] = "fallback"
				result["fallback_reason"] = f"{type(exc).__name__}: {exc}"
			if on_event:
				source_count = sum(len(topic.get("results", [])) for topic in result.get("topics", []))
				on_event(company, "completed", f"ReAct Observation：获得 {source_count} 条来源，资料已交付汇总")
			return result

	results = await asyncio.gather(*(run_one(company) for company in company_names))
	return list(results)


def research_companies(
	companies: list[str],
	config: dict[str, Any],
	on_event: ResearchEventCallback | None = None,
	max_workers: int = 4,
) -> list[dict[str, Any]]:
	"""为同步调用方运行异步研究协调器。"""
	return asyncio.run(research_companies_async(companies, config, on_event, max_workers))


__all__ = ["research_companies", "research_companies_async", "research_question"]
