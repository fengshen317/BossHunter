"""BossHunter 统一后端进程入口。"""

from __future__ import annotations

import os
from pathlib import Path

from bosshunter.browser import configure
from bosshunter.config import load_config

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8686


def resolve_base_dir(value: str | None = None) -> Path:
	"""解析后端运行目录。

	Args:
		value: 显式指定的运行目录；为空时依次检查环境变量、源码根目录和当前目录。

	Returns:
		包含配置及运行数据的绝对目录。
	"""
	configured = value or os.getenv("BOSSHUNTER_BASE_DIR")
	if configured:
		return Path(configured).expanduser().resolve()

	source_root = Path(__file__).resolve().parents[2]
	if (source_root / "config.yaml").exists():
		return source_root
	return Path.cwd().resolve()


def run_backend(
	*,
	host: str = DEFAULT_HOST,
	port: int = DEFAULT_PORT,
	base_dir: str | Path | None = None,
	open_browser: bool = False,
) -> None:
	"""初始化共享依赖并启动统一 Web 后端。

	Args:
		host: HTTP 监听地址。
		port: HTTP 监听端口。
		base_dir: ``config.yaml`` 和 ``data`` 所在目录。
		open_browser: 启动后是否打开管理界面。
	"""
	from bosshunter.web.server import run_server, set_base_dir

	runtime_dir = resolve_base_dir(str(base_dir) if base_dir is not None else None)
	config_path = runtime_dir / "config.yaml"
	set_base_dir(runtime_dir)
	# 浏览器运行时与 Web API 必须读取同一份配置，避免不同入口产生行为差异。
	configure(load_config(config_path))
	run_server(host=host, port=port, open_browser=open_browser)


def main() -> None:
	"""从环境变量读取进程配置并启动后端。"""
	host = os.getenv("BOSSHUNTER_HOST", DEFAULT_HOST)
	port = int(os.getenv("BOSSHUNTER_PORT", str(DEFAULT_PORT)))
	# 后端服务默认只监听端口，避免双击启动脚本时打断用户并自动拉起浏览器。
	open_browser = os.getenv("BOSSHUNTER_OPEN_BROWSER", "false").strip().lower() in {"1", "true", "yes"}
	run_backend(host=host, port=port, open_browser=open_browser)


if __name__ == "__main__":
	main()
