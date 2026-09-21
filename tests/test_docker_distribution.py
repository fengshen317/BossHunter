"""Windows Docker 分发文件的安全约束测试。"""

from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]


def load_compose() -> dict:
	"""读取 Compose 配置。"""
	return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_compose_contains_only_app_and_postgres() -> None:
	compose = load_compose()

	assert set(compose["services"]) == {"app", "postgres"}
	assert compose["services"]["app"]["ports"] == ["127.0.0.1:8686:8686"]
	assert "ports" not in compose["services"]["postgres"]


def test_compose_connects_to_host_connector_and_persists_data() -> None:
	compose = load_compose()
	app = compose["services"]["app"]

	assert app["environment"]["BOSSHUNTER_BROWSER_PROXY_HOST"] == "host.docker.internal"
	assert app["volumes"] == ["./docker-data:/app/runtime"]
	assert "postgres-data" in compose["volumes"]
	assert "healthcheck" in app
	assert "healthcheck" in compose["services"]["postgres"]


def test_start_script_checks_docker_then_starts_connector_then_compose() -> None:
	source = (ROOT / "docker-start.cmd").read_text(encoding="utf-8")

	docker_check = source.index("docker info")
	connector_start = source.index('"%CONNECTOR%" start')
	compose_start = source.index("docker compose --env-file .env.docker up")
	health_check = source.index("curl.exe -fsS")
	assert docker_check < connector_start < compose_start < health_check
	assert "taskkill" not in source.lower()


def test_stop_script_preserves_volume_and_browser() -> None:
	source = (ROOT / "docker-stop.cmd").read_text(encoding="utf-8").lower()

	assert "docker compose --env-file .env.docker down" in source
	assert " down -v" not in source
	assert "taskkill" not in source
	assert '"%connector%" stop' in source
