"""通用 DAG 并行调度器回归测试。"""

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import yaml

from bosshunter.assistant.dag_scheduler import DagScheduler, DagStep
from bosshunter.assistant.service import AssistantService


def _step(step_id: str, dependencies: list[str] | None = None, **overrides: object) -> DagStep:
	"""创建最小调度步骤。"""
	step: DagStep = {
		"id": step_id,
		"label": step_id,
		"capability": step_id,
		"depends_on": dependencies or [],
		"arguments": {},
		"requires_approval": False,
		"timeout_seconds": 1.0,
	}
	step.update(overrides)  # type: ignore[typeddict-item]
	return step


def test_scheduler_runs_ready_steps_in_parallel_and_passes_dependency_results() -> None:
	"""同一就绪层应并行执行，下一层能读取全部依赖结果。"""
	running = 0
	peak_running = 0

	async def execute(step: DagStep, dependencies: dict[str, object]) -> object:
		"""记录并发峰值，并验证汇总步骤的依赖输入。"""
		nonlocal running, peak_running
		running += 1
		peak_running = max(peak_running, running)
		await asyncio.sleep(0.02)
		running -= 1
		if step["id"] == "summary":
			return sorted(dependencies.values())
		return step["id"]

	result = asyncio.run(
		DagScheduler(max_concurrency=2).run(
			[_step("a"), _step("b"), _step("summary", ["a", "b"])],
			execute,
		)
	)

	assert peak_running == 2
	assert result["status"] == "completed"
	assert result["steps"]["summary"]["result"] == ["a", "b"]


def test_scheduler_skips_only_descendants_of_failed_step() -> None:
	"""步骤失败后只跳过依赖它的后代，独立分支仍应完成。"""
	async def execute(step: DagStep, dependencies: dict[str, object]) -> object:
		"""模拟一条失败分支。"""
		if step["id"] == "failed":
			raise RuntimeError("boom")
		return {"dependencies": dependencies}

	result = asyncio.run(
		DagScheduler().run(
			[_step("failed"), _step("blocked", ["failed"]), _step("independent")],
			execute,
		)
	)

	assert result["status"] == "failed"
	assert result["steps"]["failed"]["status"] == "failed"
	assert result["steps"]["blocked"]["status"] == "skipped"
	assert result["steps"]["independent"]["status"] == "completed"


def test_scheduler_stops_approval_branch_without_executing_it() -> None:
	"""审批步骤不得交给能力执行器，依赖分支应保持等待状态。"""
	executed: list[str] = []

	async def execute(step: DagStep, dependencies: dict[str, object]) -> object:
		"""记录实际执行的步骤。"""
		executed.append(step["id"])
		return dependencies

	result = asyncio.run(
		DagScheduler().run(
			[
				_step("prepare"),
				_step("approve", ["prepare"], requires_approval=True),
				_step("send", ["approve"]),
			],
			execute,
		)
	)

	assert executed == ["prepare"]
	assert result["status"] == "awaiting_approval"
	assert result["steps"]["approve"]["status"] == "awaiting_approval"
	assert result["steps"]["send"]["status"] == "pending"


def test_scheduler_rejects_cycles_before_execution() -> None:
	"""循环依赖必须在任何能力执行前被拒绝。"""
	async def execute(step: DagStep, dependencies: dict[str, object]) -> object:
		"""循环计划不应调用该函数。"""
		raise AssertionError("不应执行")

	result = asyncio.run(
		DagScheduler().run([_step("a", ["b"]), _step("b", ["a"])], execute)
	)

	assert result["status"] == "invalid"
	assert "循环" in str(result["error"])


def test_scheduler_enforces_step_timeout() -> None:
	"""单步骤超过预算后应失败，调度器不能永久挂起。"""
	async def execute(step: DagStep, dependencies: dict[str, object]) -> object:
		"""模拟超时能力。"""
		await asyncio.sleep(0.05)
		return dependencies

	result = asyncio.run(
		DagScheduler().run([_step("slow", timeout_seconds=0.01)], execute)
	)

	assert result["steps"]["slow"]["status"] == "failed"
	assert result["steps"]["slow"]["error"] == "步骤执行超时"


def test_langgraph_executes_multi_company_plan_through_generic_dag(tmp_path: Path) -> None:
	"""复杂公司研究应经过通用 DAG，而不是专用串行 ReAct 分支。"""
	config = {
		"profile": {"resume_path": "./resume.md"},
		"ai": {"provider": "anthropic", "model": "test-model", "api_key": "test"},
	}
	(tmp_path / "config.yaml").write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
	(tmp_path / "resume.md").write_text("# 简历", encoding="utf-8")
	intent_json = json.dumps(
		{
			"goal": "对比甲公司和乙公司",
			"intent_type": "company_research",
			"entities": {"company": ["甲公司", "乙公司"]},
			"constraints": {},
			"missing_fields": [],
			"confidence": 0.95,
			"risk_level": "low",
			"reason": "需要并行研究两家公司",
		},
		ensure_ascii=False,
	)
	running = 0
	peak_running = 0

	async def research(companies: list[str], agent_config: dict[str, object]) -> list[dict[str, object]]:
		"""模拟可观察到的并行公司研究。"""
		nonlocal running, peak_running
		assert agent_config
		running += 1
		peak_running = max(peak_running, running)
		await asyncio.sleep(0.02)
		running -= 1
		return [{"company": companies[0], "topics": []}]

	service = AssistantService(tmp_path, tmp_path / "config.yaml")
	conversation_id = service.ensure_conversation()
	try:
		with (
			patch("bosshunter.assistant.graph.call_anthropic_text", side_effect=[intent_json, "公司对比完成。"]),
			patch("bosshunter.assistant.graph.research_companies_async", side_effect=research),
			patch("bosshunter.assistant.graph._job_context", return_value=("{}", [])),
		):
			result = service.send_message(conversation_id, "对比甲公司和乙公司")
	finally:
		service.close()

	assert result["status"] == "completed"
	assert peak_running == 2
	assert result["dag_result"]["status"] == "completed"
	assert result["dag_result"]["execution_order"] == [["company_1", "company_2"], ["company_synthesis"]]
	assert [step["status"] for step in result["execution_plan"]] == ["completed", "completed", "completed"]
