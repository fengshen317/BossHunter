"""Docker 入口行为测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest


def load_entrypoint() -> ModuleType:
	"""从仓库 Docker 目录加载入口模块。"""
	path = Path(__file__).parents[1] / "docker" / "entrypoint.py"
	spec = importlib.util.spec_from_file_location("docker_entrypoint", path)
	assert spec and spec.loader
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def test_main_prepares_database_before_starting_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	entrypoint = load_entrypoint()
	order: list[str] = []
	monkeypatch.setenv("BOSSHUNTER_DATABASE_URL", "postgresql://user:pass@postgres/db")
	monkeypatch.setenv("BOSSHUNTER_BASE_DIR", str(tmp_path / "runtime"))
	monkeypatch.setenv("BOSSHUNTER_MIGRATIONS_DIR", str(tmp_path / "migrations"))
	(tmp_path / "migrations").mkdir()
	monkeypatch.setattr(entrypoint, "wait_for_database", lambda *_args: order.append("wait"))
	monkeypatch.setattr(entrypoint, "apply_migrations", lambda *_args: order.append("migrate"))
	run = Mock(side_effect=lambda *_args, **_kwargs: order.append("server") or Mock(returncode=0))

	assert entrypoint.main(run=run) == 0
	assert order == ["wait", "migrate", "server"]


def test_main_does_not_start_server_when_migration_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	entrypoint = load_entrypoint()
	monkeypatch.setenv("BOSSHUNTER_DATABASE_URL", "postgresql://user:pass@postgres/db")
	monkeypatch.setenv("BOSSHUNTER_BASE_DIR", str(tmp_path / "runtime"))
	monkeypatch.setattr(entrypoint, "wait_for_database", lambda *_args: None)
	monkeypatch.setattr(entrypoint, "apply_migrations", Mock(side_effect=RuntimeError("migration failed")))
	run = Mock()

	with pytest.raises(RuntimeError, match="migration failed"):
		entrypoint.main(run=run)
	run.assert_not_called()


def test_writable_directory_reports_exact_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	entrypoint = load_entrypoint()
	target = tmp_path / "runtime"
	monkeypatch.setattr(Path, "write_text", Mock(side_effect=OSError("denied")))

	with pytest.raises(RuntimeError, match=str(target).replace("\\", "\\\\")):
		entrypoint.ensure_writable_directory(target)
