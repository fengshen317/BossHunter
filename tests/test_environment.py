"""根目录环境变量加载测试。"""

import os
from pathlib import Path

import pytest

from bosshunter.config import load_config
from bosshunter.environment import allows_trusted_local_port_publish, load_local_environment


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


def test_load_config_applies_docker_environment_overrides(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""Docker 注入的数据库和浏览器连接配置必须覆盖文件值。"""
	config_path = tmp_path / "config.yaml"
	config_path.write_text(
		"database:\n  url: postgresql://file-value\n"
		"browser:\n  proxy_host: 127.0.0.1\n  proxy_port: 3456\n  proxy_token: file-token\n",
		encoding="utf-8",
	)
	monkeypatch.setenv("BOSSHUNTER_DATABASE_URL", "postgresql://docker-value")
	monkeypatch.setenv("BOSSHUNTER_BROWSER_PROXY_HOST", "host.docker.internal")
	monkeypatch.setenv("BOSSHUNTER_BROWSER_PROXY_PORT", "4567")
	monkeypatch.setenv("BOSSHUNTER_BROWSER_PROXY_TOKEN", "docker-token")

	config = load_config(config_path)

	assert config["database"]["url"] == "postgresql://docker-value"
	assert config["browser"]["proxy_host"] == "host.docker.internal"
	assert config["browser"]["proxy_port"] == 4567
	assert config["browser"]["proxy_token"] == "docker-token"


def test_load_config_uses_base_dir_when_path_is_omitted(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""默认配置文件应跟随当前运行实例目录，而不是源码工作目录。"""
	(tmp_path / "config.yaml").write_text("search:\n  keywords: [Docker]\n", encoding="utf-8")
	monkeypatch.setenv("BOSSHUNTER_BASE_DIR", str(tmp_path))
	monkeypatch.delenv("BOSSHUNTER_CONFIG_PATH", raising=False)

	config = load_config()

	assert config["search"]["keywords"] == ["Docker"]


def test_explicit_config_path_environment_takes_precedence_over_base_dir(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""容器可将只读配置挂载到实例目录之外。"""
	base_dir = tmp_path / "base"
	base_dir.mkdir()
	config_path = tmp_path / "mounted.yaml"
	config_path.write_text("search:\n  keywords: [Mounted]\n", encoding="utf-8")
	monkeypatch.setenv("BOSSHUNTER_BASE_DIR", str(base_dir))
	monkeypatch.setenv("BOSSHUNTER_CONFIG_PATH", str(config_path))

	config = load_config()

	assert config["search"]["keywords"] == ["Mounted"]


def test_trusted_local_port_publish_requires_container_and_explicit_flag(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""普通主机进程不能仅凭一个便利开关绕过非回环鉴权。"""
	monkeypatch.setenv("BOSSHUNTER_TRUST_LOCAL_PORT_PUBLISH", "true")
	monkeypatch.setenv("BOSSHUNTER_CONTAINER", "false")

	assert allows_trusted_local_port_publish() is False

	monkeypatch.setenv("BOSSHUNTER_CONTAINER", "true")

	assert allows_trusted_local_port_publish() is True


def test_trusted_local_port_publish_defaults_to_disabled(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""容器环境也必须显式声明由宿主机回环端口保护。"""
	monkeypatch.setenv("BOSSHUNTER_CONTAINER", "true")
	monkeypatch.delenv("BOSSHUNTER_TRUST_LOCAL_PORT_PUBLISH", raising=False)

	assert allows_trusted_local_port_publish() is False
