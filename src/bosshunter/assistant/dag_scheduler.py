"""安全、有界的通用 DAG 并行调度引擎。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Any, Literal, TypedDict


DagStepStatus = Literal["pending", "running", "completed", "failed", "skipped", "awaiting_approval"]
DagRunStatus = Literal["completed", "failed", "cancelled", "awaiting_approval", "budget_exhausted", "invalid"]


class DagStep(TypedDict):
	"""可由通用调度器执行的标准步骤。"""

	id: str
	label: str
	capability: str
	depends_on: list[str]
	arguments: dict[str, Any]
	requires_approval: bool
	timeout_seconds: float


class DagStepResult(TypedDict):
	"""单个 DAG 步骤的稳定执行结果。"""

	id: str
	status: DagStepStatus
	result: Any
	error: str | None
	duration_ms: int


class DagRunResult(TypedDict):
	"""一次 DAG 调度的完整结果。"""

	status: DagRunStatus
	steps: dict[str, DagStepResult]
	execution_order: list[list[str]]
	error: str | None
	pending_approval_step_ids: list[str]
	duration_ms: int


DagExecutor = Callable[[DagStep, dict[str, Any]], Awaitable[Any]]
DagEventCallback = Callable[[str, DagStepStatus, str], None]


class DagScheduler:
	"""按依赖就绪状态分层调度 DAG，并限制并发、超时和审批边界。"""

	def __init__(
		self,
		max_concurrency: int = 4,
		deadline_seconds: float = 120.0,
		cancel_check: Callable[[], bool] | None = None,
	) -> None:
		"""初始化调度预算。

		Args:
			max_concurrency: 同时执行的最大步骤数。
			deadline_seconds: 整个 DAG 的最长运行时间。
	"""
		self.max_concurrency = max(1, max_concurrency)
		self.deadline_seconds = max(0.01, deadline_seconds)
		self.cancel_check = cancel_check

	async def run(
		self,
		steps: list[DagStep],
		executor: DagExecutor,
		on_event: DagEventCallback | None = None,
	) -> DagRunResult:
		"""执行 DAG 中所有安全可运行的步骤。

		审批节点只会标记为等待，不会传给执行器。有失败依赖的步骤会被跳过，
		但互不依赖的分支仍可继续，以便保留尽可能多的有效结果。

		Args:
			steps: 已解析参数的 DAG 步骤。
			executor: 单能力异步执行函数。
			on_event: 步骤状态变化回调。

		Returns:
			包含所有步骤结果、并行批次和整体状态的调度结果。
	"""
		started = monotonic()
		validation_error = _validate_steps(steps)
		results = {step["id"]: _pending_result(step["id"]) for step in steps}
		if validation_error:
			return _run_result("invalid", results, [], validation_error, started)

		steps_by_id = {step["id"]: step for step in steps}
		execution_order: list[list[str]] = []
		semaphore = asyncio.Semaphore(self.max_concurrency)
		while True:
			if self.cancel_check and self.cancel_check():
				for step in steps:
					if results[step["id"]]["status"] == "pending":
						results[step["id"]].update({"status": "skipped", "error": "用户取消任务"})
						_emit(on_event, step["id"], "skipped", "用户取消任务")
				return _run_result("cancelled", results, execution_order, "用户取消任务", started)
			_propagate_failed_dependencies(steps, results, on_event)
			ready = [
				step
				for step in steps
				if results[step["id"]]["status"] == "pending"
				and all(results[dependency]["status"] == "completed" for dependency in step["depends_on"])
			]
			approval_steps = [step for step in ready if step["requires_approval"]]
			for step in approval_steps:
				results[step["id"]]["status"] = "awaiting_approval"
				_emit(on_event, step["id"], "awaiting_approval", "等待人工审批")

			runnable = [step for step in ready if not step["requires_approval"]]
			if runnable:
				remaining = self.deadline_seconds - (monotonic() - started)
				if remaining <= 0:
					return _run_result("budget_exhausted", results, execution_order, "DAG 执行超过整体时限", started)
				execution_order.append([step["id"] for step in runnable])
				try:
					await asyncio.wait_for(
						asyncio.gather(
							*(self._run_step(step, steps_by_id, results, executor, semaphore, on_event) for step in runnable)
						),
						timeout=remaining,
					)
				except TimeoutError:
					for step in runnable:
						result = results[step["id"]]
						if result["status"] == "running":
							result.update({"status": "failed", "error": "DAG 整体执行超时"})
							_emit(on_event, step["id"], "failed", "DAG 整体执行超时")
					return _run_result("budget_exhausted", results, execution_order, "DAG 执行超过整体时限", started)
				continue

			if any(result["status"] == "awaiting_approval" for result in results.values()):
				return _run_result("awaiting_approval", results, execution_order, None, started)
			if all(result["status"] in {"completed", "failed", "skipped"} for result in results.values()):
				status: DagRunStatus = "failed" if any(
					result["status"] == "failed" for result in results.values()
				) else "completed"
				return _run_result(status, results, execution_order, None, started)
			return _run_result("invalid", results, execution_order, "存在无法调度的步骤", started)

	async def _run_step(
		self,
		step: DagStep,
		steps_by_id: dict[str, DagStep],
		results: dict[str, DagStepResult],
		executor: DagExecutor,
		semaphore: asyncio.Semaphore,
		on_event: DagEventCallback | None,
	) -> None:
		"""在并发和单步骤超时保护下执行一个就绪步骤。"""
		async with semaphore:
			started = monotonic()
			result = results[step["id"]]
			result["status"] = "running"
			_emit(on_event, step["id"], "running", step["label"])
			dependency_results = {
				dependency: results[dependency]["result"]
				for dependency in step["depends_on"]
				if dependency in steps_by_id
			}
			try:
				value = await asyncio.wait_for(
					executor(step, dependency_results),
					timeout=max(0.01, step["timeout_seconds"]),
				)
				result.update(
					{"status": "completed", "result": value, "error": None, "duration_ms": _duration_ms(started)}
				)
				_emit(on_event, step["id"], "completed", "步骤执行完成")
			except TimeoutError:
				result.update({"status": "failed", "error": "步骤执行超时", "duration_ms": _duration_ms(started)})
				_emit(on_event, step["id"], "failed", "步骤执行超时")
			except Exception as exc:
				error = f"{type(exc).__name__}: {exc}"
				result.update({"status": "failed", "error": error, "duration_ms": _duration_ms(started)})
				_emit(on_event, step["id"], "failed", error)


def _validate_steps(steps: list[DagStep]) -> str | None:
	"""验证步骤标识、依赖引用和循环。"""
	if not steps:
		return "DAG 不能为空"
	step_ids = [step["id"] for step in steps]
	if len(step_ids) != len(set(step_ids)):
		return "DAG 包含重复步骤 ID"
	known = set(step_ids)
	if any(dependency not in known for step in steps for dependency in step["depends_on"]):
		return "DAG 包含不存在的依赖步骤"
	indegree = {step["id"]: len(set(step["depends_on"])) for step in steps}
	dependents: dict[str, list[str]] = {step_id: [] for step_id in step_ids}
	for step in steps:
		for dependency in set(step["depends_on"]):
			dependents[dependency].append(step["id"])
	ready = [step_id for step_id, degree in indegree.items() if degree == 0]
	visited = 0
	while ready:
		step_id = ready.pop()
		visited += 1
		for dependent in dependents[step_id]:
			indegree[dependent] -= 1
			if indegree[dependent] == 0:
				ready.append(dependent)
	return None if visited == len(steps) else "DAG 存在循环依赖"


def _propagate_failed_dependencies(
	steps: list[DagStep],
	results: dict[str, DagStepResult],
	on_event: DagEventCallback | None,
) -> None:
	"""反复标记失败分支的全部后代。"""
	changed = True
	while changed:
		changed = False
		for step in steps:
			result = results[step["id"]]
			if result["status"] != "pending":
				continue
			if any(results[dependency]["status"] in {"failed", "skipped"} for dependency in step["depends_on"]):
				result.update({"status": "skipped", "error": "前置依赖失败"})
				_emit(on_event, step["id"], "skipped", "前置依赖失败")
				changed = True


def _pending_result(step_id: str) -> DagStepResult:
	"""创建未执行步骤结果。"""
	return {"id": step_id, "status": "pending", "result": None, "error": None, "duration_ms": 0}


def _run_result(
	status: DagRunStatus,
	steps: dict[str, DagStepResult],
	execution_order: list[list[str]],
	error: str | None,
	started: float,
) -> DagRunResult:
	"""构建稳定的整体调度结果。"""
	return {
		"status": status,
		"steps": steps,
		"execution_order": execution_order,
		"error": error,
		"pending_approval_step_ids": [
			step_id for step_id, result in steps.items() if result["status"] == "awaiting_approval"
		],
		"duration_ms": _duration_ms(started),
	}


def _duration_ms(started: float) -> int:
	"""计算单调时钟耗时。"""
	return max(0, int((monotonic() - started) * 1000))


def _emit(callback: DagEventCallback | None, step_id: str, status: DagStepStatus, detail: str) -> None:
	"""发送可选的调度事件。"""
	if callback:
		callback(step_id, status, detail)


__all__ = ["DagRunResult", "DagScheduler", "DagStep", "DagStepResult"]
