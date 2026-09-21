"""加载项目根目录的本地环境变量文件。"""

from __future__ import annotations

import os
from pathlib import Path


def _environment_path() -> Path:
	"""返回当前运行实例应读取的 ``.env`` 路径。"""
	configured = os.getenv("BOSSHUNTER_BASE_DIR", "").strip()
	if configured:
		return Path(configured).expanduser().resolve() / ".env"
	return Path.cwd().resolve() / ".env"


def load_local_environment(path: Path | None = None) -> None:
	"""读取简单 KEY=VALUE 文件，且不覆盖进程中已设置的变量。

	Args:
		path: 显式环境文件路径；为空时使用项目运行根目录下的 ``.env``。
	"""
	environment_path = path or _environment_path()
	if not environment_path.is_file():
		return
	for raw_line in environment_path.read_text(encoding="utf-8").splitlines():
		line = raw_line.strip()
		if not line or line.startswith("#") or "=" not in line:
			continue
		key, value = line.split("=", 1)
		key = key.strip()
		if not key or not key.replace("_", "").isalnum():
			continue
		value = value.strip()
		if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
			value = value[1:-1]
		os.environ.setdefault(key, value)


__all__ = ["load_local_environment"]
