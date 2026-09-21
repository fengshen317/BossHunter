"""BossHunter - BOSS直聘智能求职Agent"""

import os
import sys

from bosshunter.environment import load_local_environment


# LangGraph/LangSmith 在导入时读取追踪环境变量，因此必须早于其它业务模块加载。
load_local_environment()

# Windows: force UTF-8 for console output
if sys.platform == "win32":
    os.environ.setdefault("PYTHONUTF8", "1")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

__version__ = "2.0.0"
