"""Agent 生产基础设施的回归测试。"""

from pathlib import Path

import pytest

from bosshunter.infrastructure.artifacts import LocalArtifactStore
from bosshunter.infrastructure.coordination import LocalCoordinator, RedisCoordinator, create_coordinator
from bosshunter.infrastructure.runtime_store import PostgresRuntimeStore, SQLiteRuntimeStore, create_runtime_store
from bosshunter.infrastructure.worker import execute_registered_task, register_task, task_may_retry


def test_runtime_store_persists_run_dag_observation_and_idempotent_proposal(tmp_path: Path) -> None:
	"""关键运行事实应持久化，审批提案只能消费一次。"""
	store = SQLiteRuntimeStore(tmp_path / "runtime.db")
	run_id = store.create_run("conversation-1", {"goal": "比较岗位"})
	step = {"id": "query", "capability": "job_query", "arguments": {}, "depends_on": [], "status": "pending"}
	store.save_step(run_id, step, {"status": "completed", "result": {"count": 2}, "error": None})
	observation_id = store.save_observation(run_id, "query", {"summary": "获得 2 条结果"})
	proposal_id = store.create_proposal(
		run_id,
		{"id": "proposal-1", "capability": "send_greetings", "arguments": {"job_ids": ["job-1"]}},
	)

	assert observation_id
	assert proposal_id == "proposal-1"
	assert store.consume_proposal(proposal_id, {"job_ids": ["job-1"]}) is True
	assert store.consume_proposal(proposal_id, {"job_ids": ["job-1"]}) is False
	stored = store.get_run(run_id)
	assert stored is not None
	assert stored["status"] == "running"
	assert stored["steps"][0]["status"] == "completed"
	assert store.latest_run_id("conversation-1") == run_id


def test_runtime_store_factory_requires_postgresql(tmp_path: Path) -> None:
	"""缺少 PostgreSQL URL 时必须快速失败，禁止静默回退 SQLite。"""
	with pytest.raises(RuntimeError, match="PostgreSQL"):
		create_runtime_store({}, tmp_path)


def test_runtime_store_factory_selects_postgresql(tmp_path: Path) -> None:
	"""配置 PostgreSQL URL 时应选择 PostgreSQL 仓储。"""
	store = create_runtime_store({"database": {"url": "postgresql://local/test"}}, tmp_path)
	assert isinstance(store, PostgresRuntimeStore)


def test_local_coordinator_supports_lock_cancel_and_events() -> None:
	"""单机降级实现应保持与 Redis 协议一致的关键语义。"""
	coordinator = LocalCoordinator()
	with coordinator.lock("conversation:1", timeout_seconds=0) as acquired:
		assert acquired is True
		with coordinator.lock("conversation:1", timeout_seconds=0) as duplicate:
			assert duplicate is False
	coordinator.request_cancel("run-1")
	event_id = coordinator.publish_event("run-1", {"status": "running"})

	assert coordinator.is_cancelled("run-1") is True
	assert coordinator.events("run-1") == [{"id": event_id, "status": "running"}]


def test_coordinator_factory_defaults_to_local_without_redis() -> None:
	"""单进程桌面配置不应依赖 Redis 服务或分布式锁租约。"""
	coordinator = create_coordinator({"runtime": {"redis_url": "", "redis_required": False}})

	assert isinstance(coordinator, LocalCoordinator)


def test_coordinator_factory_requires_url_for_explicit_multi_instance_mode() -> None:
	"""显式要求 Redis 时缺少 URL 应快速失败，防止多实例失去互斥。"""
	with pytest.raises(RuntimeError, match="redis_url"):
		create_coordinator({"runtime": {"redis_url": "", "redis_required": True}})


def test_redis_coordinator_does_not_release_lock_after_ownership_is_lost() -> None:
	"""长任务租约意外丢失时，清理阶段不能覆盖真实执行结果。"""
	class FakeLock:
		def __init__(self) -> None:
			self.release_calls = 0

		def acquire(self, blocking: bool = True) -> bool:
			return blocking

		def owned(self) -> bool:
			return False

		def extend(self, additional_time: int, replace_ttl: bool = False) -> bool:
			return additional_time > 0 and replace_ttl

		def release(self) -> None:
			self.release_calls += 1

	class FakeRedis:
		def __init__(self, fake_lock: FakeLock) -> None:
			self.fake_lock = fake_lock

		def lock(self, name: str, **kwargs: object) -> FakeLock:
			assert name == "bosshunter:lock:conversation:1"
			assert kwargs["timeout"] == 30
			return self.fake_lock

	fake_lock = FakeLock()
	coordinator = object.__new__(RedisCoordinator)
	coordinator.client = FakeRedis(fake_lock)
	coordinator.namespace = "bosshunter"

	with coordinator.lock("conversation:1", timeout_seconds=5) as acquired:
		assert acquired is True

	assert fake_lock.release_calls == 0


def test_local_artifact_store_blocks_path_escape_and_returns_checksum(tmp_path: Path) -> None:
	"""制品只能写入配置根目录，并返回可审计校验和。"""
	store = LocalArtifactStore(tmp_path / "artifacts")
	reference = store.put("runs/run-1/report.json", b'{"ok": true}', "application/json")

	assert reference["size"] == 12
	assert len(reference["checksum"]) == 64
	assert store.get(reference["storage_key"]) == b'{"ok": true}'
	with pytest.raises(ValueError, match="非法制品路径"):
		store.put("../secret.txt", b"secret")


def test_worker_executes_only_registered_tasks_and_disables_sensitive_retries() -> None:
	"""Worker 不得根据输入导入任意函数，外部发送不得自动重试。"""
	register_task("readonly-test", lambda payload: {"value": payload["value"]})

	assert execute_registered_task("readonly-test", {"value": 3}) == {"value": 3}
	assert task_may_retry("research_company") is True
	assert task_may_retry("send_hr_reply") is False
	with pytest.raises(ValueError, match="未注册后台任务"):
		execute_registered_task("os.system", {})
