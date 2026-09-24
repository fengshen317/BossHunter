"""业务数据库连接路由回归测试。"""

from pathlib import Path

import pytest

from bosshunter.db import get_business_db


def test_business_db_requires_postgresql(tmp_path: Path) -> None:
	"""未配置 PostgreSQL 时不得回退到本地文件数据库。"""
	config_path = tmp_path / "config.yaml"
	config_path.write_text("database:\n  url: ''\n", encoding="utf-8")

	with pytest.raises(RuntimeError, match="PostgreSQL"):
		get_business_db(config_path)


def test_business_db_prefers_configured_postgresql(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""配置 PostgreSQL 时应使用该连接 URL。"""
	config_path = tmp_path / "config.yaml"
	config_path.write_text("database:\n  url: postgresql://user:pass@localhost/bosshunter\n", encoding="utf-8")
	expected_connection = object()
	captured_urls: list[str] = []

	def fake_postgres_connection(url: str) -> object:
		"""记录数据库 URL，避免单元测试建立真实网络连接。"""
		captured_urls.append(url)
		return expected_connection

	monkeypatch.setattr("bosshunter.db.PostgresConnection", fake_postgres_connection)

	connection = get_business_db(config_path)

	assert connection is expected_connection
	assert captured_urls == ["postgresql://user:pass@localhost/bosshunter"]


def test_business_db_prefers_environment_database_url(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""Docker 数据库 URL 应覆盖配置文件中的连接地址。"""
	config_path = tmp_path / "config.yaml"
	config_path.write_text("database:\n  url: postgresql://file-value\n", encoding="utf-8")
	monkeypatch.setenv("BOSSHUNTER_DATABASE_URL", "postgresql://docker-value")
	captured_urls: list[str] = []

	def fake_postgres_connection(url: str) -> object:
		"""记录最终连接 URL。"""
		captured_urls.append(url)
		return object()

	monkeypatch.setattr("bosshunter.db.PostgresConnection", fake_postgres_connection)

	get_business_db(config_path)

	assert captured_urls == ["postgresql://docker-value"]
