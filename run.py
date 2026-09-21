"""BossHunter 前后端开发环境一键启动脚本。"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent
FRONTEND_DIR = PROJECT_ROOT / "src" / "bosshunter" / "web" / "frontend"
DEFAULT_BACKEND_PORT = 8686
DEFAULT_FRONTEND_PORT = 5174


def resolve_backend_python() -> Path:
	"""返回后端专用虚拟环境解释器。

	Returns:
		项目 ``.venv`` 中的 Python 路径。

	Raises:
		RuntimeError: 虚拟环境尚未创建。
	"""
	python_name = "python.exe" if os.name == "nt" else "python"
	python_path = PROJECT_ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin") / python_name
	if not python_path.is_file():
		raise RuntimeError("后端虚拟环境不存在，请先运行：python -m venv .venv && .venv\\Scripts\\python -m pip install -e .")
	return python_path


def parse_args() -> argparse.Namespace:
	"""解析开发服务器启动参数。

	Returns:
		解析后的命令行参数。
	"""
	parser = argparse.ArgumentParser(description="一键启动 BossHunter 前后端开发服务")
	parser.add_argument("--backend-port", type=int, default=DEFAULT_BACKEND_PORT, help="后端端口，默认 8686")
	parser.add_argument("--frontend-port", type=int, default=DEFAULT_FRONTEND_PORT, help="前端端口，默认 5174")
	parser.add_argument(
		"--browser",
		choices=("auto", "chrome", "edge"),
		default=None,
		help="开发浏览器，默认读取 config.yaml 的 browser.preferred_browser",
	)
	parser.add_argument("--no-browser", action="store_true", help="仅启动前后端，不启动自动化浏览器")
	return parser.parse_args()


def build_environment(backend_port: int) -> dict[str, str]:
	"""创建前后端共享的子进程环境变量。

	Args:
		backend_port: 后端 HTTP 端口。

	Returns:
		用于启动子进程的环境变量。
	"""
	environment = os.environ.copy()
	source_dir = str(PROJECT_ROOT / "src")
	existing_pythonpath = environment.get("PYTHONPATH", "")
	environment["PYTHONPATH"] = os.pathsep.join(filter(None, (source_dir, existing_pythonpath)))
	environment["BOSSHUNTER_BASE_DIR"] = str(PROJECT_ROOT)
	environment["BOSSHUNTER_HOST"] = "127.0.0.1"
	environment["BOSSHUNTER_PORT"] = str(backend_port)
	environment["BOSSHUNTER_OPEN_BROWSER"] = "false"
	environment["VITE_BACKEND_URL"] = f"http://127.0.0.1:{backend_port}"
	return environment


def validate_frontend() -> str:
	"""检查前端运行环境并返回 npm 命令路径。

	Returns:
		npm 可执行文件路径。

	Raises:
		RuntimeError: npm 或前端依赖不可用。
	"""
	npm_command = shutil.which("npm.cmd" if os.name == "nt" else "npm")
	if npm_command is None:
		raise RuntimeError("未找到 npm，请先安装 Node.js")
	if not (FRONTEND_DIR / "node_modules").is_dir():
		raise RuntimeError(f"前端依赖尚未安装，请先运行：cd {FRONTEND_DIR} && npm ci")
	return npm_command


def launch_development_browser(frontend_port: int, browser_choice: str | None = None) -> subprocess.Popen[Any]:
	"""启动带远程调试端口的项目专用浏览器。

	Args:
		frontend_port: Vite 开发服务器端口。
		browser_choice: 命令行指定的浏览器；为空时读取项目配置。

	Returns:
		浏览器主进程。

	Raises:
		RuntimeError: 配置的浏览器不可用。
	"""
	from bosshunter.browser.launcher import find_browser_executable, launch_browser
	from bosshunter.config import load_config

	config = load_config(PROJECT_ROOT / "config.yaml")
	browser_config = config.get("browser", {})
	preferred_browser = browser_choice or str(browser_config.get("preferred_browser") or "auto")
	executable = find_browser_executable(preferred_browser, browser_config.get("executable_path"))
	if executable is None:
		raise RuntimeError("未找到 Chrome 或 Edge，请检查 browser.executable_path 配置")

	configured_ports = browser_config.get("chrome_ports", [9222])
	debug_port = int(configured_ports[0] if configured_ports else 9222)
	configured_profile = Path(str(browser_config.get("user_data_dir") or "./data/browser-profile"))
	user_data_dir = configured_profile if configured_profile.is_absolute() else PROJECT_ROOT / configured_profile
	return launch_browser(
		executable=executable,
		debug_port=debug_port,
		user_data_dir=user_data_dir,
		dashboard_url=f"http://127.0.0.1:{frontend_port}",
	)


def stop_process(process: subprocess.Popen[bytes]) -> None:
	"""尽量平滑地停止子进程，超时后再强制结束。

	Args:
		process: 需要停止的子进程。
	"""
	if process.poll() is not None:
		return
	process.terminate()
	try:
		process.wait(timeout=5)
	except subprocess.TimeoutExpired:
		process.kill()
		process.wait()


def run(
	backend_port: int,
	frontend_port: int,
	*,
	open_browser: bool,
	browser_choice: str | None = None,
) -> int:
	"""启动前后端进程并等待任一进程退出。

	Args:
		backend_port: 后端 HTTP 端口。
		frontend_port: Vite 开发服务器端口。
		open_browser: 是否启动项目专用自动化浏览器。
		browser_choice: 可选的浏览器类型覆盖。

	Returns:
		进程退出码。
	"""
	npm_command = validate_frontend()
	backend_python = resolve_backend_python()
	environment = build_environment(backend_port)
	backend_command = [str(backend_python), "-m", "bosshunter"]
	frontend_command = [npm_command, "run", "dev", "--", "--host", "127.0.0.1", "--port", str(frontend_port)]

	print(f"后端地址：http://127.0.0.1:{backend_port}")
	print(f"前端地址：http://127.0.0.1:{frontend_port}")
	print("按 Ctrl+C 同时停止前后端。")
	backend = subprocess.Popen(backend_command, cwd=PROJECT_ROOT, env=environment)
	frontend = subprocess.Popen(frontend_command, cwd=FRONTEND_DIR, env=environment)
	processes = (backend, frontend)

	try:
		if open_browser:
			time.sleep(1)
			launch_development_browser(frontend_port, browser_choice)
		while True:
			for process in processes:
				exit_code = process.poll()
				if exit_code is not None:
					return exit_code
			time.sleep(0.25)
	except KeyboardInterrupt:
		return 0
	finally:
		for process in reversed(processes):
			stop_process(process)


def main() -> int:
	"""执行一键启动流程。

	Returns:
		进程退出码。
	"""
	args = parse_args()
	try:
		return run(
			args.backend_port,
			args.frontend_port,
			open_browser=not args.no_browser,
			browser_choice=args.browser,
		)
	except RuntimeError as exc:
		print(f"启动失败：{exc}", file=sys.stderr)
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
