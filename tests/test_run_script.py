"""开发环境启动脚本回归测试。"""

import importlib.util
from pathlib import Path
from unittest.mock import Mock, patch


_RUN_PATH = Path(__file__).resolve().parents[1] / "run.py"
_RUN_SPEC = importlib.util.spec_from_file_location("bosshunter_run_script", _RUN_PATH)
assert _RUN_SPEC and _RUN_SPEC.loader
run_script = importlib.util.module_from_spec(_RUN_SPEC)
_RUN_SPEC.loader.exec_module(run_script)


def test_launch_development_browser_uses_project_browser_config() -> None:
	"""开发入口应使用独立配置目录和远程调试端口启动 Chrome。"""
	config = {
		"browser": {
			"preferred_browser": "auto",
			"executable_path": "C:/Chrome/chrome.exe",
			"chrome_ports": [9229],
			"user_data_dir": "./data/dev-browser",
		}
	}
	process = Mock()
	with (
		patch("bosshunter.config.load_config", return_value=config),
		patch("bosshunter.browser.launcher.find_browser_executable", return_value=Path("C:/Chrome/chrome.exe")) as find,
		patch("bosshunter.browser.launcher.launch_browser", return_value=process) as launch,
	):
		result = run_script.launch_development_browser(5174)

	assert result is process
	find.assert_called_once_with("auto", "C:/Chrome/chrome.exe")
	launch.assert_called_once_with(
		executable=Path("C:/Chrome/chrome.exe"),
		debug_port=9229,
		user_data_dir=run_script.PROJECT_ROOT / "data/dev-browser",
		dashboard_url="http://127.0.0.1:5174",
	)


def test_run_starts_development_browser_by_default_path() -> None:
	"""启动前后端后应拉起可供自动化使用的浏览器。"""
	backend = Mock()
	frontend = Mock()
	backend.poll.return_value = 0
	frontend.poll.return_value = None
	backend.wait.return_value = None
	frontend.wait.return_value = None
	with (
		patch.object(run_script, "validate_frontend", return_value="npm.cmd"),
		patch.object(run_script, "resolve_backend_python", return_value=Path("D:/project/BossHunter/.venv/Scripts/python.exe")),
		patch.object(run_script.subprocess, "Popen", side_effect=[backend, frontend]),
		patch.object(run_script, "launch_development_browser") as launch,
		patch.object(run_script.time, "sleep"),
	):
		result = run_script.run(8686, 5174, open_browser=True, browser_choice="chrome")

	assert result == 0
	launch.assert_called_once_with(5174, "chrome")
