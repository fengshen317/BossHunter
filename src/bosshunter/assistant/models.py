"""Agent 分层架构共享的数据契约。"""

from typing import Any, Literal, NotRequired, TypedDict


IntentType = Literal[
	"job_search",
	"job_score",
	"job_query",
	"company_research",
	"greeting_generate",
	"greeting_send",
	"reply_draft",
	"reply_send",
	"reply_monitor",
	"pipeline_run",
	"complex_task",
	"general_question",
]
"""理解层允许输出的有限意图集合。"""

RiskLevel = Literal["low", "medium", "high"]
"""意图对应的操作风险等级。"""

RouteName = Literal["clarification", "direct", "capability", "planner"]
"""决策路由器允许选择的固定分支。"""


class IntentResult(TypedDict):
	"""理解层输出的结构化意图。"""

	goal: str
	intent_type: IntentType
	entities: dict[str, Any]
	constraints: dict[str, Any]
	missing_fields: list[str]
	confidence: float
	risk_level: RiskLevel
	reason: str
	steps: NotRequired[list[dict[str, Any]]]


class ContextSnapshot(TypedDict):
	"""上下文解析节点产出的最小可信上下文。"""

	question: str
	recent_messages: list[dict[str, Any]]
	current_job_id: str | None
	web_enabled: bool


class CapabilitySpec(TypedDict):
	"""能力注册表中的单项元数据。"""

	name: str
	description: str
	tool_name: str | None
	intents: list[IntentType]
	required_fields: list[str]
	risk_level: RiskLevel
	read_only: bool
	requires_approval: bool


class CapabilityMatch(TypedDict):
	"""能力匹配节点的输出。"""

	capability: str
	tool_name: str | None
	arguments: dict[str, Any]
	risk_level: RiskLevel
	read_only: bool
	requires_approval: bool


class PlanStep(TypedDict):
	"""复杂任务 DAG 中的一个可执行步骤。"""

	id: str
	label: str
	capability: str
	depends_on: list[str]
	status: str
	requires_approval: bool


class PlanEvaluation(TypedDict):
	"""计划评估节点的输出。"""

	valid: bool
	reason: str
	failed_step_id: str | None


class ReactAction(TypedDict):
	"""ReAct 推理节点选择的下一项只读动作。"""

	tool_name: str
	arguments: dict[str, Any]
	reason: str


class ReactObservation(TypedDict):
	"""ReAct 工具节点返回给推理节点的观察。"""

	tool_name: str
	arguments: dict[str, Any]
	status: Literal["completed", "failed"]
	result: Any


__all__ = [
	"CapabilityMatch",
	"CapabilitySpec",
	"ContextSnapshot",
	"IntentResult",
	"IntentType",
	"PlanEvaluation",
	"PlanStep",
	"ReactAction",
	"ReactObservation",
	"RiskLevel",
	"RouteName",
]
