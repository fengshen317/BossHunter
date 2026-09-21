"""后台 Worker 的安全任务入口。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


TaskHandler = Callable[[dict[str, Any]], dict[str, Any]]
_TASK_HANDLERS: dict[str, TaskHandler] = {}
_NON_RETRYABLE_TASKS = {"send_greetings", "send_hr_reply", "run_pipeline"}


def register_task(name: str, handler: TaskHandler) -> None:
	"""注册显式任务处理器，禁止 Worker 执行任意模块路径。"""
	if not name or name.startswith("_"):
		raise ValueError("任务名称无效")
	_TASK_HANDLERS[name] = handler


def execute_registered_task(name: str, payload: dict[str, Any]) -> dict[str, Any]:
	"""执行白名单任务；敏感任务的重试策略由调用方固定控制。"""
	handler = _TASK_HANDLERS.get(name)
	if handler is None:
		raise ValueError(f"未注册后台任务：{name}")
	return handler(dict(payload))


def task_may_retry(name: str) -> bool:
	"""只有无外部发送副作用的任务允许自动重试。"""
	return name not in _NON_RETRYABLE_TASKS


def configure_dramatiq(redis_url: str) -> Any:
	"""配置 Dramatiq Redis Broker，并返回通用 Actor。

	Args:
		redis_url: Redis 连接地址。

	Returns:
		可通过 ``send(name, payload)`` 投递的 Dramatiq Actor。
	"""
	import dramatiq
	from dramatiq.brokers.redis import RedisBroker

	broker = RedisBroker(url=redis_url)
	dramatiq.set_broker(broker)

	@dramatiq.actor(max_retries=0, time_limit=10 * 60 * 1000)
	def run_task(name: str, payload: dict[str, Any]) -> dict[str, Any]:
		"""执行已注册任务；外层按任务类型决定是否重新投递。"""
		return execute_registered_task(name, payload)

	return run_task


__all__ = ["configure_dramatiq", "execute_registered_task", "register_task", "task_may_retry"]
