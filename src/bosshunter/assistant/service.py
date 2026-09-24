"""Web API 与 LangGraph 之间的应用服务。"""

import asyncio
import json
from pathlib import Path
from threading import Lock
from collections.abc import Callable
from typing import Any

from langgraph.types import Command

from bosshunter.assistant.execution_events import upsert_execution_event
from bosshunter.assistant.graph import CANCEL_CHECK_CALLBACK, EXECUTION_EVENT_CALLBACK, TOKEN_CALLBACK, build_assistant_graph
from bosshunter.assistant.repository import (
	add_message,
	create_conversation,
	ensure_conversation_row,
	init_assistant_tables,
	save_pending_intent,
)
from bosshunter.config import load_config
from bosshunter.db import get_business_db
from bosshunter.infrastructure.coordination import RuntimeCoordinator, create_coordinator
from bosshunter.infrastructure.artifacts import ArtifactStore, create_artifact_store
from bosshunter.infrastructure.runtime_store import RuntimeStore, create_runtime_store


class AssistantService:
	"""管理单进程中的 LangGraph 和 PostgreSQL 连接。"""

	def __init__(
		self,
		base_dir: Path,
		config_path: Path,
		action_dispatcher: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
	) -> None:
		"""初始化服务。

		Args:
			base_dir: BossHunter 运行目录。
			config_path: 当前配置文件。
		"""
		self.base_dir = base_dir
		self.config_path = config_path
		self._lock = Lock()
		# psycopg 异步连接在 Windows 上不支持 ProactorEventLoop，显式使用 Selector。
		self._event_loop = asyncio.SelectorEventLoop()
		self._action_dispatcher = action_dispatcher
		self._consumed_actions: set[str] = set()
		infrastructure_config = load_config(config_path)
		self._runtime_store: RuntimeStore = create_runtime_store(infrastructure_config, base_dir)
		self._coordinator: RuntimeCoordinator = create_coordinator(infrastructure_config)
		self._artifact_store: ArtifactStore = create_artifact_store(infrastructure_config, base_dir)
		self._active_run_id: str | None = None
		self.graph, self._checkpoint_connection = build_assistant_graph(
			self.config_path,
			self.base_dir,
			self._dispatch_action,
			self._event_loop,
		)

	def _dispatch_action(self, action: dict[str, Any]) -> dict[str, Any]:
		"""确保同一个审批提案最多启动一次后台任务。"""
		action_id = str(action.get("action_id", ""))
		if not action_id or action_id in self._consumed_actions:
			raise ValueError("该审批提案已处理或已过期")
		if self._action_dispatcher is None:
			raise RuntimeError("任务调度服务不可用")
		# 先完成全部前置校验再消费提案：若 dispatch 抛错（如缺少简历），提案仍
		# 保持 pending，用户可修正后重新批准，而不是被永久标记 consumed。
		dispatcher = self._action_dispatcher
		result = dispatcher(action)
		self._runtime_store.consume_proposal(action_id, action.get("parameters") or action.get("arguments") or {})
		self._consumed_actions.add(action_id)
		return result

	def close(self) -> None:
		"""关闭 checkpoint 连接。"""
		# 注入内存 checkpoint 时（测试）没有真实连接需要关闭。
		if self._checkpoint_connection is not None:
			self._event_loop.run_until_complete(self._checkpoint_connection.close())
		self._event_loop.close()

	def _business_db(self) -> Any:
		"""打开与 Web API 完全相同的业务数据库。"""
		conn = get_business_db(self.config_path)
		init_assistant_tables(conn)
		return conn

	def ensure_conversation(self, conversation_id: str | None = None) -> str:
		"""确保存在会话并返回 ID。

		传入的 conversation_id 可能来自浏览器 localStorage 中数据库迁移前的
		残留（当前 PG 中不存在），这里补建会话行实现自愈，而不是把无效 ID
		原样透传给消息写入（会触发外键约束）。
		"""
		conn = self._business_db()
		try:
			if conversation_id:
				ensure_conversation_row(conn, conversation_id)
				return conversation_id
			return str(create_conversation(conn)["id"])
		finally:
			conn.close()

	def send_message(
		self,
		conversation_id: str,
		message: str,
		job_id: str | None = None,
		web_enabled: bool = False,
		on_token: Callable[[str], None] | None = None,
		on_event: Callable[[dict[str, Any]], None] | None = None,
		on_run: Callable[[str], None] | None = None,
	) -> dict[str, Any]:
		"""执行一轮对话，并持久化回答及可审计的执行轨迹。"""
		conn = self._business_db()
		try:
			# 会话 ID 可能是浏览器残留的旧值（当前库中不存在），先幂等补建，
			# 避免首条消息写入触发 assistant_messages 外键约束。
			ensure_conversation_row(conn, conversation_id)
			user_message = add_message(conn, conversation_id, "user", message)
		finally:
			conn.close()
		run_id = self._runtime_store.create_run(conversation_id, {"user_request": message, "job_id": job_id})
		if on_run:
			on_run(run_id)

		config = {"configurable": {"thread_id": run_id, "user_id": "local"}}
		execution_events: list[dict[str, Any]] = []
		execution_event_history: list[dict[str, Any]] = []

		def handle_event(event: dict[str, Any]) -> None:
			"""保存节点最新状态，并把每次状态变化同步转发给流式接口。"""
			normalized = upsert_execution_event(execution_events, event)
			execution_event_history.append(dict(normalized))
			self._coordinator.publish_event(run_id, normalized)
			if on_event:
				on_event(normalized)

		with self._coordinator.lock(f"conversation:{conversation_id}", timeout_seconds=5) as acquired:
			if not acquired:
				self._runtime_store.update_run(run_id, "failed", error="同一会话已有任务运行")
				raise RuntimeError("同一会话已有 Agent 任务运行")
			return self._run_message(
				conversation_id, str(user_message["id"]), message, job_id, web_enabled, on_token, execution_events, config, run_id,
				handle_event, execution_event_history,
			)

	def _run_message(
		self,
		conversation_id: str,
		user_message_id: str,
		message: str,
		job_id: str | None,
		web_enabled: bool,
		on_token: Callable[[str], None] | None,
		execution_events: list[dict[str, Any]],
		config: dict[str, Any],
		run_id: str,
		handle_event: Callable[[dict[str, Any]], None],
		execution_event_history: list[dict[str, Any]],
	) -> dict[str, Any]:
		"""在会话锁内执行 LangGraph 并可靠结束运行记录。"""
		with self._lock:
			self._active_run_id = run_id
			callback_token = TOKEN_CALLBACK.set(on_token)
			event_callback_token = EXECUTION_EVENT_CALLBACK.set(handle_event)
			cancel_callback_token = CANCEL_CHECK_CALLBACK.set(lambda: self._coordinator.is_cancelled(run_id))
			try:
				result = self._event_loop.run_until_complete(self.graph.ainvoke(
					{
						"messages": [{"role": "user", "content": message}],
						"conversation_id": conversation_id,
						"user_message_id": user_message_id,
						"job_id": job_id,
						"web_enabled": web_enabled,
					},
					config=config,
				))
			except Exception as exc:
				self._runtime_store.update_run(run_id, "failed", error=f"{type(exc).__name__}: {exc}")
				raise
			finally:
				TOKEN_CALLBACK.reset(callback_token)
				EXECUTION_EVENT_CALLBACK.reset(event_callback_token)
				CANCEL_CHECK_CALLBACK.reset(cancel_callback_token)
				self._active_run_id = None
		result["execution_events"] = execution_events
		result["_execution_event_history"] = execution_event_history
		result["run_id"] = run_id
		if self._coordinator.is_cancelled(run_id):
			# 用户主动打断时不落库助手消息，由前端保存打断前的部分回答。
			return {
				"run_id": run_id,
				"conversation_id": conversation_id,
				"message": None,
				"status": "cancelled",
			}
		self._persist_runtime_result(run_id, result)
		return self._persist_result(conversation_id, result)

	def resume(self, conversation_id: str, decision: dict[str, Any]) -> dict[str, Any]:
		"""恢复一个等待人工确认的 LangGraph 会话。"""
		run_id = self._runtime_store.latest_run_id(conversation_id)
		if not run_id:
			raise ValueError("没有可恢复的 Agent 运行")
		config = {"configurable": {"thread_id": run_id, "user_id": "local"}}
		with self._lock:
			self._active_run_id = run_id
			try:
				result = self._event_loop.run_until_complete(self.graph.ainvoke(Command(resume=decision), config=config))
			except Exception as exc:
				# 与 send_message 一致：恢复失败也要更新运行记录，避免 run 永久
				# 停留在 awaiting_approval，用户看到"已处理但未启动"的死卡片。
				if run_id:
					self._runtime_store.update_run(run_id, "failed", error=f"{type(exc).__name__}: {exc}")
				raise
			finally:
				self._active_run_id = None
		if run_id:
			result["run_id"] = run_id
			self._persist_runtime_result(run_id, result)
		return self._persist_result(conversation_id, result)

	def cancel(self, run_id: str) -> None:
		"""请求任务在下一个安全点停止，并持久化取消状态。"""
		self._coordinator.request_cancel(run_id)
		self._runtime_store.update_run(run_id, "cancelled")

	def _persist_runtime_result(self, run_id: str, result: dict[str, Any]) -> None:
		"""保存 DAG、Observation 和审批事实，供恢复与审计使用。"""
		dag_steps = result.get("dag_result", {}).get("steps", {})
		for step in result.get("execution_plan", []):
			self._runtime_store.save_step(run_id, step, dag_steps.get(step.get("id")))
		for index, observation in enumerate(result.get("react_observations", [])):
			step_id = f"observation-{index + 1}"
			serialized = json.dumps(observation.get("result"), ensure_ascii=False, default=str).encode("utf-8")
			stored_observation = dict(observation)
			if len(serialized) > 16_384:
				reference = self._artifact_store.put(
					f"runs/{run_id}/{step_id}.json",
					serialized,
					"application/json",
				)
				self._runtime_store.save_artifact(run_id, step_id, reference)
				stored_observation["result"] = None
				stored_observation["data_ref"] = reference["storage_key"]
			self._runtime_store.save_observation(run_id, step_id, stored_observation)
		interrupts = result.get("__interrupt__", ())
		approval = getattr(interrupts[0], "value", None) if interrupts else None
		if approval:
			self._runtime_store.create_proposal(run_id, approval)
			self._runtime_store.update_run(run_id, "awaiting_approval")
			return
		dag_status = str(result.get("dag_result", {}).get("status") or "completed")
		self._runtime_store.update_run(run_id, "failed" if dag_status in {"failed", "invalid"} else dag_status)

	def _persist_result(self, conversation_id: str, result: dict[str, Any]) -> dict[str, Any]:
		"""保存本次新增的最后一条助手消息并序列化中断。"""
		messages = result.get("messages", [])
		last = messages[-1] if messages else {"content": "AI 暂时没有返回内容。", "message_type": "text"}
		content = str(last.get("content", ""))
		message_type = str(last.get("message_type", "text"))
		interrupts = result.get("__interrupt__", ())
		approval = None
		if interrupts:
			approval = getattr(interrupts[0], "value", None)
			message_type = "approval"
		metadata = {
			"run_id": result.get("run_id"),
			"citations": result.get("citations", []),
			"approval": approval,
			"task": result.get("task"),
			"tool_trace": result.get("tool_trace", []),
			"execution_plan": result.get("execution_plan", []),
			"execution_events": result.get("_execution_event_history", result.get("execution_events", [])),
			"intent": result.get("intent"),
			"result_summary": result.get("result_summary", {}),
			"capabilities": [item.get("capability") for item in result.get("capability_matches", [])],
			"react_observations": result.get("react_observations", []),
			"evidence_route": result.get("evidence_route"),
			"evidence_tasks": result.get("evidence_tasks", []),
			"evidence_results": result.get("evidence_results", []),
			"evidence_coverage": result.get("evidence_coverage", {}),
			"plan_evaluation": result.get("plan_evaluation"),
			"dag_result": result.get("dag_result"),
			"replan_count": result.get("replan_count", 0),
			"context_usage": (result.get("context") or {}).get("context_usage", {}),
			"memory_updates": result.get("memory_updates", []),
		}
		conn = self._business_db()
		try:
			saved = add_message(conn, conversation_id, "assistant", content, message_type, metadata)
			# 澄清问题必须跨独立 run 保存；正常完成后立即清空，避免旧槽位污染新请求。
			save_pending_intent(
				conn,
				conversation_id,
				result.get("intent") if message_type == "clarification" else None,
			)
		finally:
			conn.close()
		return {
			"run_id": result.get("run_id"),
			"conversation_id": conversation_id,
			"message": saved,
			"citations": result.get("citations", []),
			"approval": approval,
			"task": result.get("task"),
			"tool_trace": result.get("tool_trace", []),
			"execution_plan": result.get("execution_plan", []),
			"execution_events": result.get("execution_events", []),
			"intent": result.get("intent"),
			"result_summary": result.get("result_summary", {}),
			"capabilities": [item.get("capability") for item in result.get("capability_matches", [])],
			"react_observations": result.get("react_observations", []),
			"evidence_route": result.get("evidence_route"),
			"evidence_tasks": result.get("evidence_tasks", []),
			"evidence_results": result.get("evidence_results", []),
			"evidence_coverage": result.get("evidence_coverage", {}),
			"plan_evaluation": result.get("plan_evaluation"),
			"dag_result": result.get("dag_result"),
			"replan_count": result.get("replan_count", 0),
			"status": "interrupted" if approval else "completed",
		}
