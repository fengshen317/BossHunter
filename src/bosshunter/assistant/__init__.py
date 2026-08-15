"""BossHunter 对话 Agent。"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
	from bosshunter.assistant.service import AssistantService


def __getattr__(name: str) -> Any:
	"""按需加载依赖 LangGraph 的应用服务。

	Args:
		name: 外部请求的模块属性名。

	Returns:
		延迟导入的 AssistantService 类。

	Raises:
		AttributeError: 请求了未导出的属性。
	"""
	if name == "AssistantService":
		from bosshunter.assistant.service import AssistantService

		return AssistantService
	raise AttributeError(name)

__all__ = ["AssistantService"]
