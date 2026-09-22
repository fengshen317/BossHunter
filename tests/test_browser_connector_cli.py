"""Windows Browser Connector distribution tests."""

from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path


CONNECTOR = Path(__file__).parents[1] / "src" / "bosshunter" / "browser" / "runtime" / "browser-connector.mjs"


def free_port() -> int:
	"""分配一个不会与本机 Connector 冲突的测试端口。"""
	with socket.socket() as sock:
		sock.bind(("127.0.0.1", 0))
		return int(sock.getsockname()[1])


def run_connector(tmp_path: Path, command: str, *args: str) -> subprocess.CompletedProcess[str]:
	"""Run the source connector without depending on a built EXE."""
	return subprocess.run(
		["node", str(CONNECTOR), command, *args],
		cwd=tmp_path,
		env={
			**os.environ,
			"PROGRAMFILES": str(tmp_path),
			"PROGRAMFILES(X86)": str(tmp_path),
			"LOCALAPPDATA": str(tmp_path),
		},
		capture_output=True,
		text=True,
		encoding="utf-8",
		timeout=10,
		check=False,
	)


def test_init_creates_high_entropy_secrets(tmp_path: Path) -> None:
	result = run_connector(tmp_path, "init")
	content = (tmp_path / ".env.docker").read_text(encoding="utf-8")

	assert result.returncode == 0
	values = dict(line.split("=", 1) for line in content.splitlines() if line and not line.startswith("#"))
	assert len(values["POSTGRES_PASSWORD"]) >= 40
	assert len(values["BOSSHUNTER_BROWSER_PROXY_TOKEN"]) >= 40


def test_init_does_not_overwrite_existing_credentials(tmp_path: Path) -> None:
	env_file = tmp_path / ".env.docker"
	env_file.write_text("POSTGRES_PASSWORD=keep-me\n", encoding="utf-8")

	result = run_connector(tmp_path, "init")

	assert result.returncode == 0
	assert env_file.read_text(encoding="utf-8") == "POSTGRES_PASSWORD=keep-me\n"


def test_start_reports_missing_browser_without_leaking_token(tmp_path: Path) -> None:
	(tmp_path / ".env.docker").write_text(
		"POSTGRES_PASSWORD=test\n"
		"BOSSHUNTER_BROWSER_PROXY_TOKEN=top-secret-token\n"
		f"BOSSHUNTER_BROWSER_PROXY_PORT={free_port()}\n",
		encoding="utf-8",
	)

	result = run_connector(tmp_path, "start", "--browser-path", str(tmp_path / "missing-browser.exe"))

	assert result.returncode == 1
	assert "未找到 Chrome 或 Edge" in result.stderr
	assert "top-secret-token" not in result.stderr


def test_connector_source_uses_isolated_profile_and_does_not_kill_port_owner() -> None:
	source = CONNECTOR.read_text(encoding="utf-8")

	assert "path.join(dataDir, 'browser-profile')" in source
	assert "端口 ${proxyPort} 已被占用，不会结束现有进程" in source
	assert "已被其他程序占用，但不是可用的浏览器 CDP" in source
	assert "browser.unref()" in source
	assert "process.kill(pid)" in source
