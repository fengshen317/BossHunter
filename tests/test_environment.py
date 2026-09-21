"""根目录环境变量加载测试。"""

import os
from pathlib import Path

import pytest

from bosshunter.environment import load_local_environment


def test_load_local_environment_reads_langsmith_settings_without_overwriting_process_env(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""本地配置应支持注释和引号，并以进程环境变量为最高优先级。"""
	env_path = tmp_path / ".env"
	env_path.write_text(
		"# tracing\nLANGSMITH_TRACING=true\nLANGSMITH_API_KEY='local-key'\nLANGSMITH_PROJECT=LocalProject\n",
		encoding="utf-8",
	)
	monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
	monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
	monkeypatch.setenv("LANGSMITH_PROJECT", "ProcessProject")

	load_local_environment(env_path)

	assert os.environ["LANGSMITH_TRACING"] == "true"
	assert os.environ["LANGSMITH_API_KEY"] == "local-key"
	assert os.environ["LANGSMITH_PROJECT"] == "ProcessProject"
