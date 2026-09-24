"""共享测试夹具。

迁移到 PostgreSQL 后，生产代码有三处刻意的强约束，测试必须显式替换而不是
放宽它们（放宽等于让"重启后审批会话消失"这类问题重新变得可能）：

  - ``create_runtime_store``：运行时仓储，明确拒绝 SQLite 降级
  - ``graph.create_checkpointer``：LangGraph 会话检查点，SQLite/内存无法跨进程恢复
  - ``get_business_db``：业务库，跑在用例自己的临时 SQLite 上

``local_runtime_store`` 把这三处一起替换掉，供需要真实 AssistantService 的用例复用。
"""

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from bosshunter.db import get_db
from bosshunter.infrastructure.runtime_store import SQLiteRuntimeStore


async def memory_checkpointer(config_path: Path):
	"""内存 checkpoint：测试不依赖 PostgreSQL，也无需跨进程恢复会话。"""
	from langgraph.checkpoint.memory import InMemorySaver

	return None, InMemorySaver()


def web_db_from_base_dir():
	"""把 web 层业务库指向当前 ``server.BASE_DIR`` 下的 SQLite。

	生产 ``server._get_web_db`` 读 BASE_DIR/config.yaml 里的 database.url
	（必须是 PostgreSQL）；用例用临时目录装 SQLite，两者对不上，接口会直接 500。

	只在临时库确实存在时才重定向：用例若忘了准备数据库就抛明确错误，
	而不是在仓库里凭空造一个 data/bosshunter.db。
	"""
	from bosshunter.web import server

	db_path = server.BASE_DIR / "data" / "bosshunter.db"
	if not db_path.exists():
		raise RuntimeError(f"用例未准备业务库：{db_path}")
	return get_db(db_path)


@contextmanager
def local_runtime_store(base_dir: Path):
	"""让 AssistantService 及其 LangGraph 图在纯本地依赖下运行。

	Args:
		base_dir: 用例的临时运行目录，业务库取 ``base_dir/data/bosshunter.db``。
	"""
	db_path = base_dir / "data" / "bosshunter.db"
	local_db = lambda config_path: get_db(db_path)  # noqa: E731 - 补丁替身，无需命名

	with (
		patch(
			"bosshunter.assistant.service.create_runtime_store",
			return_value=SQLiteRuntimeStore(base_dir / "data" / "runtime.db"),
		),
		patch("bosshunter.assistant.graph.create_checkpointer", memory_checkpointer),
		# graph.py 与 service.py 各自 from bosshunter.db import get_business_db，
		# 名字在导入时就绑定了，必须逐个模块替换。
		patch("bosshunter.assistant.graph.get_business_db", local_db),
		patch("bosshunter.assistant.service.get_business_db", local_db),
	):
		yield
