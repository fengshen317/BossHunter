"""Browser Runtime HTTP 鉴权集成测试。"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

import httpx


SCRIPT = Path(__file__).parents[1] / "src" / "bosshunter" / "browser" / "runtime" / "cdp-proxy.mjs"


def free_port() -> int:
	"""分配一个短生命周期测试端口。"""
	with socket.socket() as sock:
		sock.bind(("127.0.0.1", 0))
		return int(sock.getsockname()[1])


def wait_for_health(port: int) -> dict:
	"""等待 Node Runtime 开始监听。"""
	deadline = time.monotonic() + 5
	while time.monotonic() < deadline:
		try:
			response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.2, trust_env=False)
			if response.status_code == 200:
				return response.json()
		except httpx.HTTPError:
			pass
		time.sleep(0.05)
	raise AssertionError("Browser Runtime 未在期限内启动")


def test_non_loopback_listener_requires_token() -> None:
	port = free_port()
	environment = {
		**os.environ,
		"BOSSHUNTER_BROWSER_PROXY_HOST": "0.0.0.0",
		"BOSSHUNTER_BROWSER_PROXY_PORT": str(port),
		"BOSSHUNTER_BROWSER_PROXY_TOKEN": "",
	}
	result = subprocess.run(
		["node", str(SCRIPT)],
		env=environment,
		capture_output=True,
		text=True,
		encoding="utf-8",
		timeout=5,
		check=False,
	)

	assert result.returncode != 0
	assert "Non-loopback listener requires" in result.stderr


def test_runtime_rejects_bad_token_and_keeps_health_minimal() -> None:
	port = free_port()
	secret = "integration-secret"
	environment = {
		**os.environ,
		"BOSSHUNTER_BROWSER_PROXY_HOST": "127.0.0.1",
		"BOSSHUNTER_BROWSER_PROXY_PORT": str(port),
		"BOSSHUNTER_BROWSER_PROXY_TOKEN": secret,
		"BOSSHUNTER_CHROME_PORTS": str(free_port()),
	}
	process = subprocess.Popen(
		["node", str(SCRIPT)],
		env=environment,
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
		text=True,
		encoding="utf-8",
	)
	try:
		health = wait_for_health(port)
		unauthorized = httpx.get(f"http://127.0.0.1:{port}/targets", timeout=2, trust_env=False)
		authorized = httpx.get(
			f"http://127.0.0.1:{port}/targets",
			headers={"Authorization": f"Bearer {secret}"},
			timeout=2,
			trust_env=False,
		)

		assert set(health) == {"status", "runtime", "connected"}
		assert unauthorized.status_code == 401
		assert secret not in unauthorized.text
		assert authorized.status_code != 401
	finally:
		process.terminate()
		process.wait(timeout=5)
