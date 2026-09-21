"""Agent 多实例协调、取消信号和实时事件接口。"""

from __future__ import annotations

import json
from contextlib import contextmanager
from threading import Event, Lock, Thread
from time import monotonic
from typing import Any, Iterator, Protocol


class RuntimeCoordinator(Protocol):
	"""运行时协调协议，Redis 不可用时可由进程内实现降级。"""

	@contextmanager
	def lock(self, key: str, timeout_seconds: int = 30) -> Iterator[bool]:
		"""获取有界互斥锁。"""

	def request_cancel(self, run_id: str) -> None:
		"""设置取消信号。"""

	def is_cancelled(self, run_id: str) -> bool:
		"""读取取消信号。"""

	def publish_event(self, run_id: str, event: dict[str, Any]) -> str:
		"""发布实时事件并返回事件 ID。"""


class LocalCoordinator:
	"""单机模式下的进程内协调器。"""

	def __init__(self) -> None:
		"""初始化锁、取消集合和事件缓冲。"""
		self._guard = Lock()
		self._locks: dict[str, Lock] = {}
		self._cancelled: set[str] = set()
		self._events: dict[str, list[dict[str, Any]]] = {}

	@contextmanager
	def lock(self, key: str, timeout_seconds: int = 30) -> Iterator[bool]:
		"""获取进程内命名锁，超时后返回 False。"""
		with self._guard:
			lock = self._locks.setdefault(key, Lock())
		acquired = lock.acquire(timeout=max(0, timeout_seconds))
		try:
			yield acquired
		finally:
			if acquired:
				lock.release()

	def request_cancel(self, run_id: str) -> None:
		"""标记运行需要在下一个安全点停止。"""
		with self._guard:
			self._cancelled.add(run_id)

	def is_cancelled(self, run_id: str) -> bool:
		"""检查运行是否被取消。"""
		with self._guard:
			return run_id in self._cancelled

	def publish_event(self, run_id: str, event: dict[str, Any]) -> str:
		"""将事件追加到本地缓冲。"""
		event_id = f"{int(monotonic() * 1000)}-{len(self._events.get(run_id, []))}"
		with self._guard:
			self._events.setdefault(run_id, []).append({"id": event_id, **event})
		return event_id

	def events(self, run_id: str) -> list[dict[str, Any]]:
		"""返回本地事件快照。"""
		with self._guard:
			return [dict(event) for event in self._events.get(run_id, [])]


class RedisCoordinator:
	"""基于 Redis Lock、String 和 Stream 的多实例协调器。"""

	def __init__(self, url: str, namespace: str = "bosshunter") -> None:
		"""创建 Redis 客户端；连接在首次操作时建立。"""
		from redis import Redis

		self.client = Redis.from_url(url, decode_responses=True)
		self.namespace = namespace

	def _key(self, *parts: str) -> str:
		"""生成隔离的 Redis Key。"""
		return ":".join((self.namespace, *parts))

	@contextmanager
	def lock(self, key: str, timeout_seconds: int = 30) -> Iterator[bool]:
		"""获取自动续租的分布式锁，避免长任务结束时释放已过期租约。"""
		lease_seconds = max(30, timeout_seconds * 6)
		lock = self.client.lock(
			self._key("lock", key),
			timeout=lease_seconds,
			blocking_timeout=timeout_seconds,
		)
		acquired = bool(lock.acquire(blocking=True))
		stop_renewal = Event()

		def renew_lock() -> None:
			"""在任务执行期间周期续租；所有权丢失后停止，不能续租他人的锁。"""
			while not stop_renewal.wait(lease_seconds / 3):
				try:
					if not lock.owned():
						return
					lock.extend(lease_seconds, replace_ttl=True)
				except Exception:
					# 续租线程不能把 Redis 瞬时故障传播到正在执行的 Agent 线程。
					return

		renewal_thread = Thread(target=renew_lock, name=f"redis-lock-renew-{key}", daemon=True)
		if acquired:
			renewal_thread.start()
		try:
			yield acquired
		finally:
			stop_renewal.set()
			if acquired:
				renewal_thread.join(timeout=1)
				try:
					if lock.owned():
						lock.release()
				except Exception:
					# Redis 故障或租约已经丢失时，不让清理异常覆盖 Agent 的真实执行结果。
					pass

	def request_cancel(self, run_id: str) -> None:
		"""写入一小时有效的取消信号。"""
		self.client.set(self._key("run", run_id, "cancel"), "1", ex=3600)

	def is_cancelled(self, run_id: str) -> bool:
		"""检查取消信号。"""
		return self.client.exists(self._key("run", run_id, "cancel")) == 1

	def publish_event(self, run_id: str, event: dict[str, Any]) -> str:
		"""发布到 Redis Stream，并限制保留长度。"""
		payload = {"payload": json.dumps(event, ensure_ascii=False, default=str)}
		return str(self.client.xadd(self._key("run", run_id, "events"), payload, maxlen=2000, approximate=True))


def create_coordinator(config: dict[str, Any]) -> RuntimeCoordinator:
	"""按部署模式创建协调器，单进程默认使用无租约的本地锁。"""
	runtime = config.get("runtime", {}) if isinstance(config, dict) else {}
	redis_url = str(runtime.get("redis_url") or "").strip()
	redis_required = bool(runtime.get("redis_required", False))
	if not redis_url:
		if redis_required:
			raise RuntimeError("runtime.redis_required=true 时必须配置 runtime.redis_url")
		return LocalCoordinator()
	try:
		coordinator = RedisCoordinator(redis_url)
		coordinator.client.ping()
		return coordinator
	except Exception:
		if redis_required:
			raise
		return LocalCoordinator()


__all__ = ["LocalCoordinator", "RedisCoordinator", "RuntimeCoordinator", "create_coordinator"]
