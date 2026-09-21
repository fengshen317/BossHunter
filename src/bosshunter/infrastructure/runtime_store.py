"""Agent Run、DAG、Observation 和审批的持久化仓储。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Protocol


class RuntimeStore(Protocol):
	"""Agent 运行时持久化协议。"""

	def create_run(self, conversation_id: str, goal: dict[str, Any]) -> str:
		"""创建运行记录。"""

	def update_run(self, run_id: str, status: str, *, error: str | None = None) -> None:
		"""更新运行状态。"""

	def save_step(self, run_id: str, step: dict[str, Any], result: dict[str, Any] | None = None) -> None:
		"""幂等保存 DAG 步骤及结果。"""

	def save_observation(self, run_id: str, step_id: str, observation: dict[str, Any]) -> str:
		"""保存工具观察。"""

	def create_proposal(self, run_id: str, proposal: dict[str, Any]) -> str:
		"""保存审批提案。"""

	def consume_proposal(self, proposal_id: str, approved_arguments: dict[str, Any]) -> bool:
		"""原子消费审批提案。"""

	def get_run(self, run_id: str) -> dict[str, Any] | None:
		"""读取运行及其步骤。"""

	def latest_run_id(self, conversation_id: str) -> str | None:
		"""读取会话最近一次运行 ID。"""

	def save_artifact(self, run_id: str, step_id: str, reference: dict[str, Any]) -> None:
		"""保存制品引用。"""


class SQLiteRuntimeStore:
	"""兼容本地模式的 SQLite 运行时仓储。"""

	def __init__(self, path: Path) -> None:
		"""初始化数据库路径并创建运行时表。"""
		self.path = path
		path.parent.mkdir(parents=True, exist_ok=True)
		with self._connect() as conn:
			self._init_schema(conn)

	def _connect(self) -> sqlite3.Connection:
		"""创建启用外键和行对象的短连接。"""
		conn = sqlite3.connect(str(self.path), timeout=30)
		conn.row_factory = sqlite3.Row
		conn.execute("PRAGMA foreign_keys = ON")
		conn.execute("PRAGMA busy_timeout = 30000")
		conn.execute("PRAGMA journal_mode = WAL")
		return conn

	@staticmethod
	def _init_schema(conn: sqlite3.Connection) -> None:
		"""创建可恢复 Agent 运行所需的最小事实表。"""
		conn.executescript("""
			CREATE TABLE IF NOT EXISTS agent_runs (
				id TEXT PRIMARY KEY,
				conversation_id TEXT NOT NULL,
				goal_json TEXT NOT NULL,
				status TEXT NOT NULL,
				error TEXT,
				created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
				updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
				finished_at TIMESTAMP
			);
			CREATE TABLE IF NOT EXISTS agent_plan_steps (
				id TEXT NOT NULL,
				run_id TEXT NOT NULL,
				capability TEXT NOT NULL,
				arguments_json TEXT NOT NULL DEFAULT '{}',
				dependencies_json TEXT NOT NULL DEFAULT '[]',
				status TEXT NOT NULL,
				result_json TEXT,
				error TEXT,
				idempotency_key TEXT NOT NULL,
				updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
				PRIMARY KEY (run_id, id),
				UNIQUE (idempotency_key),
				FOREIGN KEY (run_id) REFERENCES agent_runs(id)
			);
			CREATE TABLE IF NOT EXISTS agent_observations (
				id TEXT PRIMARY KEY,
				run_id TEXT NOT NULL,
				step_id TEXT NOT NULL,
				observation_json TEXT NOT NULL,
				created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
				FOREIGN KEY (run_id) REFERENCES agent_runs(id)
			);
			CREATE TABLE IF NOT EXISTS action_proposals (
				id TEXT PRIMARY KEY,
				run_id TEXT NOT NULL,
				capability TEXT NOT NULL,
				arguments_json TEXT NOT NULL,
				approved_arguments_json TEXT,
				status TEXT NOT NULL DEFAULT 'pending',
				expires_at TEXT,
				consumed_at TIMESTAMP,
				created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
				FOREIGN KEY (run_id) REFERENCES agent_runs(id)
			);
			CREATE TABLE IF NOT EXISTS artifacts (
				id TEXT PRIMARY KEY, run_id TEXT, step_id TEXT, storage_key TEXT NOT NULL UNIQUE,
				content_type TEXT NOT NULL, size INTEGER NOT NULL, checksum TEXT NOT NULL,
				metadata_json TEXT NOT NULL DEFAULT '{}', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
				FOREIGN KEY (run_id) REFERENCES agent_runs(id)
			);
			CREATE INDEX IF NOT EXISTS idx_agent_runs_conversation ON agent_runs(conversation_id, created_at);
			CREATE INDEX IF NOT EXISTS idx_agent_steps_run ON agent_plan_steps(run_id, status);
			CREATE INDEX IF NOT EXISTS idx_agent_observations_run ON agent_observations(run_id, step_id);
		""")
		conn.commit()

	def create_run(self, conversation_id: str, goal: dict[str, Any]) -> str:
		"""创建运行记录并返回不可预测 ID。"""
		run_id = str(uuid.uuid4())
		with self._connect() as conn:
			conn.execute(
				"INSERT INTO agent_runs (id, conversation_id, goal_json, status) VALUES (?, ?, ?, 'running')",
				(run_id, conversation_id, _json(goal)),
			)
		return run_id

	def update_run(self, run_id: str, status: str, *, error: str | None = None) -> None:
		"""更新状态，并为终态记录完成时间。"""
		terminal = status in {"completed", "failed", "cancelled", "budget_exhausted"}
		with self._connect() as conn:
			conn.execute(
				"""UPDATE agent_runs SET status = ?, error = ?, updated_at = CURRENT_TIMESTAMP,
				finished_at = CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE finished_at END WHERE id = ?""",
				(status, error, terminal, run_id),
			)

	def save_step(self, run_id: str, step: dict[str, Any], result: dict[str, Any] | None = None) -> None:
		"""按 run_id 和 step_id 更新步骤，保留稳定幂等键。"""
		step_id = str(step["id"])
		idempotency_key = str(step.get("idempotency_key") or f"{run_id}:{step_id}")
		status = str((result or {}).get("status") or step.get("status") or "pending")
		with self._connect() as conn:
			conn.execute(
				"""INSERT INTO agent_plan_steps
				(id, run_id, capability, arguments_json, dependencies_json, status, result_json, error, idempotency_key)
				VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
				ON CONFLICT(run_id, id) DO UPDATE SET status = excluded.status,
				result_json = excluded.result_json, error = excluded.error, updated_at = CURRENT_TIMESTAMP""",
				(
					step_id, run_id, str(step["capability"]), _json(step.get("arguments", {})),
					_json(step.get("depends_on", [])), status, _json((result or {}).get("result")),
					(result or {}).get("error"), idempotency_key,
				),
			)

	def save_observation(self, run_id: str, step_id: str, observation: dict[str, Any]) -> str:
		"""保存标准 Observation 并返回 ID。"""
		observation_id = str(uuid.uuid4())
		with self._connect() as conn:
			conn.execute(
				"INSERT INTO agent_observations (id, run_id, step_id, observation_json) VALUES (?, ?, ?, ?)",
				(observation_id, run_id, step_id, _json(observation)),
			)
		return observation_id

	def create_proposal(self, run_id: str, proposal: dict[str, Any]) -> str:
		"""保存参数冻结的审批提案。"""
		proposal_id = str(proposal.get("id") or proposal.get("action_id") or uuid.uuid4())
		with self._connect() as conn:
			conn.execute(
				"""INSERT OR IGNORE INTO action_proposals
				(id, run_id, capability, arguments_json, expires_at) VALUES (?, ?, ?, ?, ?)""",
				(
					proposal_id, run_id, str(proposal.get("capability") or proposal.get("type") or "unknown"),
					_json(proposal.get("arguments") or proposal.get("parameters") or {}), proposal.get("expires_at"),
				),
			)
		return proposal_id

	def consume_proposal(self, proposal_id: str, approved_arguments: dict[str, Any]) -> bool:
		"""仅允许 pending 提案被原子消费一次。"""
		with self._connect() as conn:
			cursor = conn.execute(
				"""UPDATE action_proposals SET status = 'consumed', approved_arguments_json = ?,
				consumed_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'pending'""",
				(_json(approved_arguments), proposal_id),
			)
		return cursor.rowcount == 1

	def get_run(self, run_id: str) -> dict[str, Any] | None:
		"""读取运行及步骤，供恢复和测试使用。"""
		with self._connect() as conn:
			run = conn.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
			if not run:
				return None
			steps = conn.execute("SELECT * FROM agent_plan_steps WHERE run_id = ? ORDER BY rowid", (run_id,)).fetchall()
		return {**dict(run), "goal": json.loads(run["goal_json"]), "steps": [dict(step) for step in steps]}

	def latest_run_id(self, conversation_id: str) -> str | None:
		"""读取 SQLite 会话最近运行。"""
		with self._connect() as conn:
			row = conn.execute(
				"SELECT id FROM agent_runs WHERE conversation_id = ? ORDER BY created_at DESC LIMIT 1",
				(conversation_id,),
			).fetchone()
		return str(row["id"]) if row else None

	def save_artifact(self, run_id: str, step_id: str, reference: dict[str, Any]) -> None:
		"""保存 SQLite 制品引用。"""
		with self._connect() as conn:
			conn.execute(
				"""INSERT OR REPLACE INTO artifacts
				(id, run_id, step_id, storage_key, content_type, size, checksum, metadata_json)
				VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
				(
					reference["id"], run_id, step_id, reference["storage_key"], reference["content_type"],
					reference["size"], reference["checksum"], _json(reference.get("metadata", {})),
				),
			)


class PostgresRuntimeStore:
	"""使用 psycopg 的 PostgreSQL 运行时仓储入口。

	完整表结构由 ``migrations/postgresql/001_agent_runtime.sql`` 管理；该类刻意
	不做自动建表，避免生产环境启动时隐式修改数据库。
	"""

	def __init__(self, url: str) -> None:
		"""保存 PostgreSQL URL，并在首次连接时延迟导入驱动。"""
		self.url = url

	def _connect(self) -> Any:
		"""延迟导入 psycopg 并创建字典行连接。"""
		import psycopg
		from psycopg.rows import dict_row

		return psycopg.connect(self.url.replace("postgresql+psycopg://", "postgresql://"), row_factory=dict_row)

	def create_run(self, conversation_id: str, goal: dict[str, Any]) -> str:
		"""创建 PostgreSQL 运行记录。"""
		run_id = str(uuid.uuid4())
		with self._connect() as conn, conn.cursor() as cursor:
			cursor.execute(
				"INSERT INTO agent_runs (id, conversation_id, goal_json, status) VALUES (%s, %s, %s::jsonb, 'running')",
				(run_id, conversation_id, _json(goal)),
			)
		return run_id

	def update_run(self, run_id: str, status: str, *, error: str | None = None) -> None:
		"""更新 PostgreSQL 运行状态。"""
		terminal = status in {"completed", "failed", "cancelled", "budget_exhausted"}
		with self._connect() as conn, conn.cursor() as cursor:
			cursor.execute(
				"""UPDATE agent_runs SET status = %s, error = %s, updated_at = CURRENT_TIMESTAMP,
				finished_at = CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE finished_at END WHERE id = %s""",
				(status, error, terminal, run_id),
			)

	def save_step(self, run_id: str, step: dict[str, Any], result: dict[str, Any] | None = None) -> None:
		"""幂等保存 PostgreSQL DAG 步骤。"""
		step_id = str(step["id"])
		status = str((result or {}).get("status") or step.get("status") or "pending")
		with self._connect() as conn, conn.cursor() as cursor:
			cursor.execute(
				"""INSERT INTO agent_plan_steps
				(id, run_id, capability, arguments_json, dependencies_json, status, result_json, error, idempotency_key)
				VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s::jsonb, %s, %s)
				ON CONFLICT(run_id, id) DO UPDATE SET status = excluded.status,
				result_json = excluded.result_json, error = excluded.error, updated_at = CURRENT_TIMESTAMP""",
				(
					step_id, run_id, str(step["capability"]), _json(step.get("arguments", {})),
					_json(step.get("depends_on", [])), status, _json((result or {}).get("result")),
					(result or {}).get("error"), str(step.get("idempotency_key") or f"{run_id}:{step_id}"),
				),
			)

	def save_observation(self, run_id: str, step_id: str, observation: dict[str, Any]) -> str:
		"""保存 PostgreSQL Observation。"""
		observation_id = str(uuid.uuid4())
		with self._connect() as conn, conn.cursor() as cursor:
			cursor.execute(
				"""INSERT INTO agent_observations (id, run_id, step_id, observation_json)
				VALUES (%s, %s, %s, %s::jsonb)""",
				(observation_id, run_id, step_id, _json(observation)),
			)
		return observation_id

	def create_proposal(self, run_id: str, proposal: dict[str, Any]) -> str:
		"""保存 PostgreSQL 审批提案。"""
		proposal_id = str(proposal.get("id") or proposal.get("action_id") or uuid.uuid4())
		with self._connect() as conn, conn.cursor() as cursor:
			cursor.execute(
				"""INSERT INTO action_proposals (id, run_id, capability, arguments_json, expires_at)
				VALUES (%s, %s, %s, %s::jsonb, %s) ON CONFLICT(id) DO NOTHING""",
				(
					proposal_id, run_id, str(proposal.get("capability") or proposal.get("type") or "unknown"),
					_json(proposal.get("arguments") or proposal.get("parameters") or {}), proposal.get("expires_at"),
				),
			)
		return proposal_id

	def consume_proposal(self, proposal_id: str, approved_arguments: dict[str, Any]) -> bool:
		"""原子消费 PostgreSQL 审批提案。"""
		with self._connect() as conn, conn.cursor() as cursor:
			cursor.execute(
				"""UPDATE action_proposals SET status = 'consumed', approved_arguments_json = %s::jsonb,
				consumed_at = CURRENT_TIMESTAMP WHERE id = %s AND status = 'pending'""",
				(_json(approved_arguments), proposal_id),
			)
			return cursor.rowcount == 1

	def get_run(self, run_id: str) -> dict[str, Any] | None:
		"""读取 PostgreSQL 运行及步骤。"""
		with self._connect() as conn, conn.cursor() as cursor:
			cursor.execute("SELECT * FROM agent_runs WHERE id = %s", (run_id,))
			run = cursor.fetchone()
			if not run:
				return None
			cursor.execute("SELECT * FROM agent_plan_steps WHERE run_id = %s ORDER BY updated_at", (run_id,))
			steps = cursor.fetchall()
		return {**run, "goal": run["goal_json"], "steps": steps}

	def latest_run_id(self, conversation_id: str) -> str | None:
		"""读取 PostgreSQL 会话最近运行。"""
		with self._connect() as conn, conn.cursor() as cursor:
			cursor.execute(
				"SELECT id FROM agent_runs WHERE conversation_id = %s ORDER BY created_at DESC LIMIT 1",
				(conversation_id,),
			)
			row = cursor.fetchone()
		return str(row["id"]) if row else None

	def save_artifact(self, run_id: str, step_id: str, reference: dict[str, Any]) -> None:
		"""保存 PostgreSQL 制品引用。"""
		with self._connect() as conn, conn.cursor() as cursor:
			cursor.execute(
				"""INSERT INTO artifacts
				(id, run_id, step_id, storage_key, content_type, size, checksum, metadata_json)
				VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
				ON CONFLICT(id) DO UPDATE SET storage_key = excluded.storage_key""",
				(
					reference["id"], run_id, step_id, reference["storage_key"], reference["content_type"],
					reference["size"], reference["checksum"], _json(reference.get("metadata", {})),
				),
			)


def create_runtime_store(config: dict[str, Any], base_dir: Path) -> RuntimeStore:
	"""根据配置创建 PostgreSQL 运行时仓储。"""
	database = config.get("database", {}) if isinstance(config, dict) else {}
	url = str(database.get("url") or "").strip()
	if url.startswith(("postgresql://", "postgresql+psycopg://")):
		return PostgresRuntimeStore(url)
	raise RuntimeError("database.url 必须配置为 PostgreSQL，已禁用 SQLite 运行时降级")


def _json(value: Any) -> str:
	"""稳定序列化数据库 JSON 字段。"""
	return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


__all__ = ["PostgresRuntimeStore", "RuntimeStore", "SQLiteRuntimeStore", "create_runtime_store"]
