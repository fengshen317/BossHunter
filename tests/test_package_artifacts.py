"""发布包关键资源声明测试。"""

from pathlib import Path


def test_dockerfile_builds_frontend_and_runs_as_non_root() -> None:
	source = (Path(__file__).parents[1] / "Dockerfile").read_text(encoding="utf-8")

	assert "npm run build" in source
	assert "USER bosshunter" in source
	assert "BOSSHUNTER_BROWSER_RUNTIME=external" in source
	assert "HEALTHCHECK" in source


def test_wheel_declares_runtime_and_frontend_assets() -> None:
	source = (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")

	assert '"src/bosshunter/browser/runtime/*.mjs"' in source
	assert '"src/bosshunter/web/frontend/dist/**"' in source
