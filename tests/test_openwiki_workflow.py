"""用临时 Git 仓库验证 OpenWiki 工作流的实际暂存路径。"""

import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github/workflows/openwiki-update.yml"


def _git(directory: Path, *arguments: str) -> str:
	"""执行临时仓库命令，失败时保留完整输出。

	Args:
		directory: 临时仓库目录。
		*arguments: Git 参数。

	Returns:
		Git 标准输出。
	"""
	result = subprocess.run(["git", *arguments], cwd=directory, capture_output=True, text=True)
	assert result.returncode == 0, result.stdout + result.stderr
	return result.stdout


@pytest.mark.parametrize("agents_state", ["missing", "present", "deleted", "ignored", "new"])
def test_openwiki_stages_only_valid_paths(tmp_path: Path, agents_state: str) -> None:
	"""可选文件缺失不影响 Wiki 更新，已跟踪删除也不能遗漏。"""
	workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
	steps = workflow["jobs"]["update"]["steps"]
	selection = next(step for step in steps if step.get("id") == "update_paths")
	create_pr = next(step for step in steps if "create-pull-request@" in step.get("uses", ""))
	assert create_pr["with"]["add-paths"] == "${{ steps.update_paths.outputs.paths }}"
	assert "steps.update_paths.outputs.paths != ''" in create_pr["if"]

	_git(tmp_path, "init")
	_git(tmp_path, "config", "user.name", "测试")
	_git(tmp_path, "config", "user.email", "test@example.invalid")
	(tmp_path / "openwiki").mkdir()
	(tmp_path / "openwiki/page.md").write_text("初始内容", encoding="utf-8")
	if agents_state == "ignored":
		(tmp_path / ".gitignore").write_text("AGENTS.md\n", encoding="utf-8")
	if agents_state in {"present", "deleted"}:
		(tmp_path / "AGENTS.md").write_text("初始规则", encoding="utf-8")
	_git(tmp_path, "add", ".")
	_git(tmp_path, "commit", "-m", "test: 创建基线")
	(tmp_path / "openwiki/page.md").write_text("更新内容", encoding="utf-8")
	if agents_state == "present":
		(tmp_path / "AGENTS.md").write_text("更新规则", encoding="utf-8")
	elif agents_state == "deleted":
		(tmp_path / "AGENTS.md").unlink()
	elif agents_state in {"new", "ignored"}:
		(tmp_path / "AGENTS.md").write_text("新规则", encoding="utf-8")
	# 无关文件不得因为动态路径选择进入暂存区。
	(tmp_path / "unrelated.txt").write_text("无关", encoding="utf-8")
	output_path = tmp_path / "github-output.txt"
	bash = shutil.which("bash")
	if os.name == "nt":
		git_path = Path(shutil.which("git") or "")
		candidate = git_path.parent.parent / "bin/bash.exe"
		if candidate.exists():
			bash = str(candidate)
	assert bash, "验证工作流需要 Bash"
	result = subprocess.run(
		[bash],
		input=selection["run"],
		cwd=tmp_path,
		text=True,
		capture_output=True,
		env={**os.environ, "GITHUB_OUTPUT": str(output_path)},
	)
	assert result.returncode == 0, result.stdout + result.stderr
	lines = output_path.read_text(encoding="utf-8").splitlines()
	assert lines[0] == "paths<<OPENWIKI_PATHS" and lines[-1] == "OPENWIKI_PATHS"
	paths = lines[1:-1]
	assert "openwiki" in paths
	assert ("AGENTS.md" in paths) == (agents_state in {"present", "deleted", "new"})
	assert "CLAUDE.md" not in paths
	_git(tmp_path, "add", "--", *paths)
	staged = _git(tmp_path, "diff", "--cached", "--name-only").splitlines()
	assert "openwiki/page.md" in staged
	assert ("AGENTS.md" in staged) == (agents_state in {"present", "deleted", "new"})
	assert "unrelated.txt" not in staged
