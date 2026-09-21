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

IntentAction = Literal[
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
]
"""可映射到注册能力的任务动作。"""

InteractionMode = Literal["answer", "execute", "draft", "confirm", "cancel"]
"""用户本轮是在咨询、执行、起草、确认还是取消。"""

RiskLevel = Literal["low", "medium", "high"]
"""意图对应的操作风险等级。"""

SideEffect = Literal["none", "local_reversible", "local_persistent", "external"]
"""能力产生的副作用类型，独立于风险等级。"""

AgentStatus = Literal[
	"running",
	"awaiting_input",
	"awaiting_approval",
	"completed",
	"budget_exhausted",
]
"""通用 Agent 的运行状态。"""

EvidenceDomain = Literal["direct", "structured_data", "private_document", "public_current", "mixed"]
"""回答问题所需的主证据域。"""

EvidenceStatus = Literal["completed", "partial", "insufficient", "failed"]
"""专业执行器返回的稳定状态。"""


class GoalState(TypedDict):
	"""不依赖唯一业务意图的通用目标状态。"""

	user_request: str
	goal: str
	success_criteria: list[str]
	constraints: list[str]
	entities: dict[str, Any]
	assumptions: list[str]
	unknowns: list[str]
	selected_job_ids: list[str]
	completion_status: str


class EvidenceRoute(TypedDict):
	"""本轮问题的证据域路由结果。"""

	mode: EvidenceDomain
	domains: list[EvidenceDomain]
	reason: str
	freshness_required: bool


class EvidenceItem(TypedDict):
	"""一条具有明确来源和时效信息的事实证据。"""

	claim: str
	value: Any
	source_type: EvidenceDomain
	source_id: str
	source_label: str
	source_location: str | None
	observed_at: str
	effective_at: str | None
	confidence: float


class EvidenceResult(TypedDict):
	"""问数、私有资料和联网执行器共享的结果协议。"""

	task_id: str
	executor: str
	status: EvidenceStatus
	evidence: list[EvidenceItem]
	summary: str
	missing_evidence: list[str]
	error: str | None
	data_ref: str | None
	data: Any


class EvidenceTask(TypedDict):
	"""复杂问题证据计划中的一个有依赖子任务。"""

	id: str
	question: str
	evidence_type: EvidenceDomain
	depends_on: list[str]
	required: bool
	max_results: int
	timeout_seconds: float
	status: str

RouteName = Literal["clarification", "direct", "capability", "planner"]
"""决策路由器允许选择的固定分支。"""


class IntentStep(TypedDict):
	"""任务理解器输出的单个标准步骤。"""

	id: str
	action: IntentAction
	arguments: dict[str, Any]
	depends_on: list[str]
	enabled: bool
	reason: str


class IntentConstraints(TypedDict, total=False):
	"""由策略层消费、不得直接传给工具的控制约束。"""

	forbidden_actions: list[IntentAction]
	requires_confirmation: bool
	do_not_send: bool


class IntentResult(TypedDict):
	"""理解层输出的结构化意图。"""

	goal: str
	intent_type: IntentType
	entities: dict[str, Any]
	constraints: IntentConstraints
	missing_fields: list[str]
	confidence: float
	risk_level: RiskLevel
	reason: str
	mode: NotRequired[InteractionMode]
	steps: NotRequired[list[IntentStep]]
	memory_operations: NotRequired[list[dict[str, Any]]]


class ContextSnapshot(TypedDict):
	"""上下文解析节点产出的最小可信上下文。"""

	question: str
	recent_messages: list[dict[str, Any]]
	conversation_summary: str
	current_job_id: str | None
	web_enabled: bool
	pending_intent: IntentResult | None
	retrieved_memories: NotRequired[list[dict[str, Any]]]
	context_usage: NotRequired[dict[str, Any]]


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
	when_to_use: list[str]
	when_not_to_use: list[str]
	input_schema: dict[str, Any]
	output_schema: dict[str, Any]
	side_effect: SideEffect
	timeout_seconds: int
	cost_level: Literal["low", "medium", "high"]
	tags: list[str]


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
	arguments: NotRequired[dict[str, Any]]
	timeout_seconds: NotRequired[float]


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
	summary: NotRequired[str]
	facts: NotRequired[dict[str, Any]]
	error: NotRequired[str | None]


class ExecutionEvent(TypedDict, total=False):
	"""供 SSE、消息持久化和前端执行记录共同消费的事件。"""

	version: int
	id: str
	parent_id: str | None
	kind: Literal["phase", "plan", "tool", "agent", "approval", "task", "result"]
	phase: Literal[
		"understanding",
		"planning",
		"retrieval",
		"generation",
		"approval",
		"execution",
		"finalization",
	]
	label: str
	status: Literal["pending", "running", "waiting", "completed", "failed", "stopping", "stopped", "skipped"]
	summary: str
	detail: str
	started_at: str
	finished_at: str
	duration_ms: int
	capability: str
	tool_name: str
	requires_approval: bool


__all__ = [
	"CapabilityMatch",
	"CapabilitySpec",
	"ContextSnapshot",
	"ExecutionEvent",
	"EvidenceDomain",
	"EvidenceItem",
	"EvidenceResult",
	"EvidenceRoute",
	"EvidenceStatus",
	"EvidenceTask",
	"AgentStatus",
	"GoalState",
	"InteractionMode",
	"IntentAction",
	"IntentConstraints",
	"IntentResult",
	"IntentStep",
	"IntentType",
	"PlanEvaluation",
	"PlanStep",
	"ReactAction",
	"ReactObservation",
	"RiskLevel",
	"RouteName",
	"SideEffect",
]
