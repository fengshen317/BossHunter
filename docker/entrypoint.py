#!/usr/bin/env python3
"""BossHunter 容器入口：验证目录、等待 PostgreSQL、执行迁移并启动 Web。"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable


def wait_for_database(database_url: str, timeout_seconds: float = 60.0) -> None:
	"""在有限时间内等待 PostgreSQL 可用。

	Args:
		database_url: psycopg 数据库连接串。
		timeout_seconds: 最大等待秒数。

	Raises:
		RuntimeError: 数据库在期限内仍不可用。
	"""
	import psycopg

	deadline = time.monotonic() + timeout_seconds
	delay = 0.5
	last_error = "unknown error"
	while time.monotonic() < deadline:
		try:
			with psycopg.connect(database_url, connect_timeout=3):
				return
		except psycopg.Error as exc:
			last_error = str(exc).splitlines()[0]
			time.sleep(delay)
			delay = min(delay * 1.7, 5.0)
	raise RuntimeError(f"PostgreSQL 在 {timeout_seconds:g} 秒内未就绪：{last_error}")


def apply_migrations(database_url: str, migrations_dir: Path) -> None:
	"""按文件名顺序执行尚未应用的 PostgreSQL 迁移。

	Args:
		database_url: psycopg 数据库连接串。
		migrations_dir: SQL 迁移目录。
	"""
	import psycopg

	if not migrations_dir.is_dir():
		raise RuntimeError(f"迁移目录不存在：{migrations_dir}")
	with psycopg.connect(database_url) as connection:
		connection.execute(
			"CREATE TABLE IF NOT EXISTS schema_migrations "
			"(name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)"
		)
		for migration in sorted(migrations_dir.glob("*.sql")):
			applied = connection.execute(
				"SELECT 1 FROM schema_migrations WHERE name = %s", (migration.name,)
			).fetchone()
			if applied:
				continue
			connection.execute(migration.read_text(encoding="utf-8"))
			connection.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (migration.name,))


def ensure_writable_directory(path: Path) -> None:
	"""确保运行目录存在且可写。

	Args:
		path: 要验证的目录。
	"""
	path.mkdir(parents=True, exist_ok=True)
	probe = path / ".write-test"
	try:
		probe.write_text("ok", encoding="utf-8")
		probe.unlink()
	except OSError as exc:
		raise RuntimeError(f"目录不可写：{path}") from exc


def main(run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> int:
	"""准备容器运行环境并以前台进程方式启动服务。

	Args:
		run: 可替换的子进程执行函数，便于单元测试。

	Returns:
		后端进程退出码。
	"""
	database_url = os.environ.get("BOSSHUNTER_DATABASE_URL", "").replace("postgresql+psycopg://", "postgresql://")
	if not database_url.startswith("postgresql://"):
		raise RuntimeError("BOSSHUNTER_DATABASE_URL 必须配置为 PostgreSQL 连接串")
	base_dir = Path(os.environ.get("BOSSHUNTER_BASE_DIR", "/app/runtime"))
	ensure_writable_directory(base_dir)
	ensure_writable_directory(base_dir / "data")
	wait_for_database(database_url, float(os.environ.get("BOSSHUNTER_DATABASE_WAIT_SECONDS", "60")))
	apply_migrations(database_url, Path(os.environ.get("BOSSHUNTER_MIGRATIONS_DIR", "/app/migrations/postgresql")))
	result = run(["bosshunter-server"], check=False)
	return int(result.returncode)


if __name__ == "__main__":
	try:
		raise SystemExit(main())
	except Exception as exc:
		print(f"[BossHunter] 容器启动失败：{exc}", file=sys.stderr)
		raise SystemExit(1) from exc
