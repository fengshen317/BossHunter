"""PostgreSQL 集成测试的独立 schema 夹具。"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row


TEST_DATABASE_URL_ENV = "BOSSHUNTER_TEST_DATABASE_URL"
MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations" / "postgresql"


def get_test_database_url() -> str:
	"""读取并校验专用于测试的 PostgreSQL 连接 URL。

	Returns:
		经过协议校验的 PostgreSQL URL。

	Raises:
		pytest.skip.Exception: 未配置测试数据库时跳过集成测试。
	"""
	url = os.environ.get(TEST_DATABASE_URL_ENV, "").strip()
	if not url:
		pytest.skip(f"未配置 {TEST_DATABASE_URL_ENV}，跳过 PostgreSQL 集成测试")
	if not url.startswith(("postgresql://", "postgresql+psycopg://")):
		raise RuntimeError(f"{TEST_DATABASE_URL_ENV} 必须为 PostgreSQL URL")
	return url.replace("postgresql+psycopg://", "postgresql://")


def _schema_name() -> str:
	"""生成限定字符集的临时 schema 名称。"""
	return f"test_bosshunter_{uuid.uuid4().hex}"


def _apply_migrations(connection: psycopg.Connection[Any]) -> None:
	"""在当前 schema 中执行全部正式 PostgreSQL 迁移。"""
	for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
		connection.execute(migration.read_text(encoding="utf-8"))
	connection.commit()


@contextmanager
def postgres_schema() -> Iterator[psycopg.Connection[Any]]:
	"""提供独立 schema 的 PostgreSQL 连接，并在结束后删除该 schema。

	Yields:
		已设置 search_path 并执行全部迁移的字典行连接。
	"""
	url = get_test_database_url()
	schema = _schema_name()
	admin = psycopg.connect(url, autocommit=True)
	connection: psycopg.Connection[Any] | None = None
	try:
		admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
		connection = psycopg.connect(url, row_factory=dict_row)
		connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
		_apply_migrations(connection)
		yield connection
	finally:
		if connection is not None:
			connection.close()
		admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
		admin.close()

