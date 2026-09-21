"""问数、私有资料和联网研究的专业执行器。"""

from bosshunter.assistant.executors.analytics import execute_analytics
from bosshunter.assistant.executors.private_docs import execute_private_documents
from bosshunter.assistant.executors.web_research import execute_web_research

__all__ = ["execute_analytics", "execute_private_documents", "execute_web_research"]
