"""统一后端入口测试。"""

from pathlib import Path
from unittest.mock import patch

from bosshunter import backend


def test_resolve_base_dir_prefers_explicit_value(tmp_path: Path) -> None:
	"""显式运行目录应优先于环境变量。"""
	with patch.dict("os.environ", {"BOSSHUNTER_BASE_DIR": str(tmp_path / "ignored")}):
		assert backend.resolve_base_dir(str(tmp_path)) == tmp_path.resolve()


def test_run_backend_initializes_shared_runtime(tmp_path: Path) -> None:
	"""后端启动前应统一设置目录、配置浏览器并启动 HTTP 服务。"""
	config = {"browser": {"chrome_ports": [9222]}}
	with (
		patch("bosshunter.backend.load_config", return_value=config) as load_config,
		patch("bosshunter.backend.configure") as configure,
		patch("bosshunter.web.server.set_base_dir") as set_base_dir,
		patch("bosshunter.web.server.run_server") as run_server,
	):
		backend.run_backend(host="0.0.0.0", port=9000, base_dir=tmp_path, open_browser=False)

	load_config.assert_called_once_with(tmp_path / "config.yaml")
	set_base_dir.assert_called_once_with(tmp_path.resolve())
	configure.assert_called_once_with(config)
	run_server.assert_called_once_with(host="0.0.0.0", port=9000, open_browser=False)


def test_main_reads_environment() -> None:
	"""无子命令入口应使用环境变量完成进程配置。"""
	values = {
		"BOSSHUNTER_HOST": "0.0.0.0",
		"BOSSHUNTER_PORT": "9001",
		"BOSSHUNTER_OPEN_BROWSER": "false",
	}
	with patch.dict("os.environ", values), patch("bosshunter.backend.run_backend") as run_backend:
		backend.main()

	run_backend.assert_called_once_with(host="0.0.0.0", port=9001, open_browser=False)


def test_main_does_not_open_browser_by_default() -> None:
	"""启动脚本未显式配置时不得自动打开浏览器。"""
	with (
		patch.dict(
			"os.environ",
			{
				"BOSSHUNTER_HOST": backend.DEFAULT_HOST,
				"BOSSHUNTER_PORT": str(backend.DEFAULT_PORT),
			},
			clear=True,
		),
		patch("bosshunter.backend.run_backend") as run_backend,
	):
		backend.main()

	run_backend.assert_called_once_with(
		host=backend.DEFAULT_HOST,
		port=backend.DEFAULT_PORT,
		open_browser=False,
	)
