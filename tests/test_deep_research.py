"""Deep Agents 只读研究层测试。"""

from threading import Barrier
from typing import Any

from bosshunter.assistant import deep_research


def test_company_workers_run_in_parallel_and_keep_input_order(monkeypatch: Any) -> None:
	"""多个公司工作者应同时启动，汇总结果仍保持用户输入顺序。"""
	barrier = Barrier(3, timeout=2)

	def fake_agent(company: str, config: dict[str, Any]) -> dict[str, Any]:
		"""用屏障证明三个工作者确实并行进入执行区。"""
		barrier.wait()
		return {"company": company, "summary": f"{company} 结果", "topics": [], "engine": "deepagents"}

	monkeypatch.setattr(deep_research, "_run_company_agent", fake_agent)

	results = deep_research.research_companies(["小米", "腾讯", "阿里"], {}, max_workers=3)

	assert [item["company"] for item in results] == ["小米", "腾讯", "阿里"]
	assert all(item["engine"] == "deepagents" for item in results)


def test_failed_deep_agent_falls_back_without_stopping_other_workers(monkeypatch: Any) -> None:
	"""单个 Deep Agent 失败时应只降级该公司，不中断整个比较任务。"""

	def fake_agent(company: str, config: dict[str, Any]) -> dict[str, Any]:
		"""模拟腾讯工作者初始化失败。"""
		if company == "腾讯":
			raise RuntimeError("模型暂不可用")
		return {"company": company, "summary": "完成", "topics": [], "engine": "deepagents"}

	def fake_fallback(company: str, config: dict[str, Any]) -> dict[str, Any]:
		"""返回可识别的兼容研究结果。"""
		return {"company": company, "summary": "降级完成", "topics": []}

	monkeypatch.setattr(deep_research, "_run_company_agent", fake_agent)
	monkeypatch.setattr(deep_research, "research_company", fake_fallback)

	results = deep_research.research_companies(["小米", "腾讯"], {}, max_workers=2)

	assert results[0]["engine"] == "deepagents"
	assert results[1]["engine"] == "fallback"
	assert "RuntimeError" in results[1]["fallback_reason"]


def test_research_events_expose_worker_status_without_private_reasoning(monkeypatch: Any) -> None:
	"""事件只展示工作者状态，不应输出模型内部推理。"""
	events: list[tuple[str, str, str]] = []

	def fake_agent(company: str, config: dict[str, Any]) -> dict[str, Any]:
		"""返回最小成功结果。"""
		return {"company": company, "summary": "完成", "topics": [], "engine": "deepagents"}

	monkeypatch.setattr(deep_research, "_run_company_agent", fake_agent)

	deep_research.research_companies(["腾讯"], {}, on_event=lambda *event: events.append(event))

	assert events == [
		("腾讯", "running", "Deep Agent 已接收独立研究任务"),
		("腾讯", "running", "ReAct Action：分析研究目标并选择公开搜索工具"),
		("腾讯", "completed", "ReAct Observation：获得 0 条来源，资料已交付汇总"),
	]
