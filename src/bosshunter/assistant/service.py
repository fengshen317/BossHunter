"""Web API 与 LangGraph 之间的应用服务。"""

import sqlite3
from pathlib import Path
from threading import Lock
from collections.abc import Callable
from typing import Any

from langgraph.types import Command

from bosshunter.assistant.execution_events import upsert_execution_event
from bosshunter.assistant.graph import EXECUTION_EVENT_CALLBACK, TOKEN_CALLBACK, build_assistant_graph
from bosshunter.assistant.repository import add_message, create_conversation, init_assistant_tables


class AssistantService:
	"""管理单进程中的 LangGraph 和 SQLite 连接。"""

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
		self.business_db_path = base_dir / "data" / "bosshunter.db"
		self.checkpoint_path = base_dir / "data" / "assistant-checkpoints.sqlite"
		self.config_path = config_path
		self._lock = Lock()
		self._action_dispatcher = action_dispatcher
		self._consumed_actions: set[str] = set()
		self.graph, self._checkpoint_connection = build_assistant_graph(
			self.business_db_path,
			self.checkpoint_path,
			self.config_path,
			self.base_dir,
			self._dispatch_action,
		)

	def _dispatch_action(self, action: dict[str, Any]) -> dict[str, Any]:
		"""确保同一个审批提案最多启动一次后台任务。"""
		action_id = str(action.get("action_id", ""))
		if not action_id or action_id in self._consumed_actions:
			raise ValueError("该审批提案已处理或已过期")
		if self._action_dispatcher is None:
			raise RuntimeError("任务调度服务不可用")
		result = self._action_dispatcher(action)
		self._consumed_actions.add(action_id)
		return result

	def close(self) -> None:
		"""关闭 checkpoint 连接。"""
		self._checkpoint_connection.close()

	def _business_db(self) -> sqlite3.Connection:
		"""打开助手业务数据库。"""
		conn = sqlite3.connect(str(self.business_db_path))
		conn.row_factory = sqlite3.Row
		init_assistant_tables(conn)
		return conn

	def ensure_conversation(self, conversation_id: str | None = None) -> str:
		"""确保存在会话并返回 ID。"""
		if conversation_id:
			return conversation_id
		conn = self._business_db()
		try:
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
		on_event: Callable[[dict[str, str]], None] | None = None,
	) -> dict[str, Any]:
		"""执行一轮对话，并持久化回答及可审计的执行轨迹。"""
		conn = self._business_db()
		try:
			add_message(conn, conversation_id, "user", message)
		finally:
			conn.close()

		config = {"configurable": {"thread_id": conversation_id, "user_id": "local"}}
		execution_events: list[dict[str, str]] = []

		def handle_event(event: dict[str, str]) -> None:
			"""保存节点最新状态，并把每次状态变化同步转发给流式接口。"""
			upsert_execution_event(execution_events, event)
			if on_event:
				on_event(event)

		with self._lock:
			callback_token = TOKEN_CALLBACK.set(on_token)
			event_callback_token = EXECUTION_EVENT_CALLBACK.set(handle_event)
			try:
				result = self.graph.invoke(
					{
						"messages": [{"role": "user", "content": message}],
						"job_id": job_id,
						"web_enabled": web_enabled,
					},
					config=config,
				)
			finally:
				TOKEN_CALLBACK.reset(callback_token)
				EXECUTION_EVENT_CALLBACK.reset(event_callback_token)
		result["execution_events"] = execution_events
		return self._persist_result(conversation_id, result)

	def resume(self, conversation_id: str, decision: dict[str, Any]) -> dict[str, Any]:
		"""恢复一个等待人工确认的 LangGraph 会话。"""
		config = {"configurable": {"thread_id": conversation_id, "user_id": "local"}}
		with self._lock:
			result = self.graph.invoke(Command(resume=decision), config=config)
		return self._persist_result(conversation_id, result)

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
			"plan_evaluation": result.get("plan_evaluation"),
			"replan_count": result.get("replan_count", 0),
		}
		conn = self._business_db()
		try:
			saved = add_message(conn, conversation_id, "assistant", content, message_type, metadata)
		finally:
			conn.close()
		return {
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
			"plan_evaluation": result.get("plan_evaluation"),
			"replan_count": result.get("replan_count", 0),
			"status": "interrupted" if approval else "completed",
		}
