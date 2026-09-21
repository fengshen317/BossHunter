# BossHunter 受控通用 Agent 架构设计

## 1. 背景

BossHunter 当前的 AI 助手以固定意图路由为主：先将用户请求识别为 `job_search`、`job_score`、`company_research` 等意图，再进入对应能力或固定流程。

这种方式适合边界明确的单一操作，但对模糊目标、多步骤任务和探索式问题存在局限：

- 用户请求经常同时包含查询、分析、研究和内容生成，难以归入唯一意图。
- 一旦入口意图识别错误，后续 Agent 就无法看到正确能力。
- 为提高识别率不断增加意图、关键词和纠偏规则，会使路由越来越复杂。
- Agent 无法根据工具的真实执行结果动态调整下一步。

目标是将 AI 助手升级为类似 OpenClaw、Hermes Agent 的开放式执行模型，同时保留 BossHunter 对发送、投递、写库和后台任务的严格安全控制。

核心原则：

> 查询、研究、分析、比较和内容生成由通用 Agent 自主完成；产生持久化或外部副作用的操作只能生成提案，经过人工确认后由固定流程执行。

## 2. 设计目标

- 不再要求每个用户请求先匹配唯一业务意图。
- 将用户自然语言转换为目标、成功标准、约束和已知实体。
- Agent 可以根据当前状态动态选择只读或纯生成能力。
- Agent 每次获得工具结果后重新判断下一步。
- 复杂任务支持按需规划、重规划和独立子任务并行。
- 所有外部副作用由确定性策略引擎拦截。
- 用户确认的动作、对象和参数必须与最终执行完全一致。
- 运行过程可审计、可中断、可恢复，并受循环次数、时间和成本预算约束。

## 3. 非目标

- 不允许 Agent 绕过人工确认直接发送招呼语、回复 HR 或投递岗位。
- 不允许通过提示词代替程序级权限控制。
- 不将完整浏览器、数据库或终端权限无条件暴露给模型。
- 第一阶段不追求完全替换现有流水线和执行器。

## 4. 总体架构

```text
                       ┌──────────────────┐
用户消息 ─────────────→│  目标与约束理解器  │
                       └────────┬─────────┘
                                │
                                ▼
                     ┌────────────────────┐
                     │ 上下文与能力检索器  │
                     │ 会话/岗位/记忆/技能 │
                     └─────────┬──────────┘
                               │
                               ▼
                  ┌─────────────────────────┐
                  │     通用 Agent Loop      │
                  │ 选择能力 → 执行 → 观察   │
                  │ → 评估 → 继续/结束       │
                  └───────┬─────────┬───────┘
                          │         │
                    只读/生成能力   有副作用能力
                          │         │
                          ▼         ▼
                    直接自主执行   ActionProposal
                                    │
                                    ▼
                              人工确认或编辑
                                    │
                                    ▼
                              固定执行工作流
                                    │
                                    ▼
                              验证并记录结果
```

架构分为三层：

1. **理解层**：提取目标和约束，不急于选择唯一业务意图。
2. **自主层**：允许 Agent 查询、研究、分析、比较和生成内容。
3. **执行层**：对写入、发送和后台任务执行确定性审批流程。

## 5. 目标理解模型

现有 `IntentResult` 可以在兼容期继续保留，但不再作为能力可见性的唯一依据。新增通用目标状态：

```python
class GoalState(TypedDict):
	"""通用 Agent 的目标状态。"""

	user_request: str
	goal: str
	success_criteria: list[str]
	constraints: list[str]
	entities: dict[str, Any]
	assumptions: list[str]
	unknowns: list[str]
	selected_job_ids: list[str]
	completion_status: str
```

示例请求：

```text
帮我看看最近的 Agent 岗位，哪些公司值得去，先整理一下，不要投递。
```

建议解析结果：

```json
{
  "goal": "整理适合用户的 Agent 岗位并分析对应公司",
  "success_criteria": [
    "获得候选岗位",
    "结合简历分析匹配度",
    "补充公司公开信息",
    "输出可比较的结果"
  ],
  "constraints": [
    "禁止投递",
    "禁止发送招呼语"
  ],
  "entities": {
    "keywords": ["Agent"]
  },
  "unknowns": [
    "本地数据库是否有足够的相关岗位"
  ]
}
```

仅在以下情况追问用户：

- 不同答案会显著改变最终结果。
- 缺失信息无法通过只读能力获得。
- 敏感操作的目标或范围不明确。
- 必须由用户作出偏好或价值判断。

工具参数暂时不完整时，应优先通过安全的只读能力补充上下文，而不是立即追问。

## 6. 统一能力注册表

现有 `assistant/capabilities.py` 从“意图到能力映射表”升级为可供 Agent 检索的能力目录。

```python
class CapabilitySpec(TypedDict):
	"""Agent 可发现和调用的能力定义。"""

	name: str
	description: str
	when_to_use: list[str]
	when_not_to_use: list[str]
	input_schema: dict[str, Any]
	output_schema: dict[str, Any]
	side_effect: str
	risk_level: str
	requires_approval: bool
	timeout_seconds: int
	cost_level: str
	tags: list[str]
```

副作用类型建议独立于风险等级定义：

```python
SideEffect = Literal[
	"none",
	"local_reversible",
	"local_persistent",
	"external",
]
```

能力分类如下：

| 能力类型 | 示例 | 默认策略 |
| --- | --- | --- |
| 只读能力 | 查询岗位、读取简历、网页搜索 | Agent 自主执行 |
| 纯生成能力 | 岗位分析、公司比较、生成草稿 | Agent 自主执行 |
| 可恢复本地写入 | 保存草稿、临时标记 | 按策略执行或生成提案 |
| 持久化本地写入 | 修改岗位状态、写入评分 | 生成提案 |
| 外部副作用 | 发送、投递、上传简历 | 强制人工确认 |
| 后台任务 | 持续监听、定时任务 | 强制人工确认 |

## 7. 动态能力检索

能力较多时，不应把全部工具定义同时传给模型。新增 `CapabilityRetriever`，根据目标、上下文和策略召回最相关的 5～8 个能力。

建议的召回流程：

1. 根据能力标签和关键词进行确定性召回。
2. 根据能力描述和用户目标进行语义召回。
3. 加入当前状态必需的能力，例如读取当前岗位。
4. 根据用户约束、权限和副作用级别过滤。
5. 对相似能力进行重排，保留最有区分度的候选。

例如“比较近期 Agent 岗位和公司情况”可召回：

- `job_query`
- `resume_read`
- `job_match_analysis`
- `company_research`
- `web_search`
- `report_generate`

若用户明确要求“不要投递”，`send_greetings` 和其他发送能力不得进入可执行候选集。

## 8. 通用 Agent Loop

Agent 每一轮只决定下一步，不要求第一次调用模型时生成完整且不可变的流程。

```python
async def run_agent(state: AgentState) -> AgentState:
	"""持续执行安全能力，直到目标完成或需要人工介入。"""

	while state["iteration"] < state["max_iterations"]:
		capabilities = retrieve_capabilities(state)
		decision = await decide_next_action(state, capabilities)

		if decision["type"] == "tool_call":
			authorization = authorize_action(decision, state)

			if authorization == "allow":
				result = await execute_capability(decision)
				state = record_observation(state, decision, result)
				continue

			if authorization == "require_approval":
				state["pending_proposal"] = build_proposal(decision, state)
				state["status"] = "awaiting_approval"
				return state

			state = record_denial(state, decision)
			continue

		if decision["type"] == "ask_user":
			state["pending_question"] = decision["question"]
			state["status"] = "awaiting_input"
			return state

		if decision["type"] == "finish":
			evaluation = evaluate_completion(state, decision)
			if evaluation["passed"]:
				state["final_answer"] = decision["answer"]
				state["status"] = "completed"
				return state
			state = append_completion_feedback(state, evaluation)

	state["status"] = "budget_exhausted"
	return state
```

模型决策应使用结构化输出：

```json
{
  "type": "tool_call",
  "tool": "job_query",
  "arguments": {
    "keywords": ["Agent"]
  },
  "reason_summary": "先检查本地是否已有相关岗位",
  "expected_evidence": "相关岗位数量和岗位详情"
}
```

工具结果统一标准化为 Observation：

```json
{
  "tool": "job_query",
  "status": "success",
  "summary": "找到 3 个相关岗位",
  "data_ref": "observation://job-query/123",
  "facts": {
    "count": 3
  },
  "error": null
}
```

大结果应保存在数据库或制品存储中，Agent 状态只保存摘要和引用，避免上下文不断膨胀。

## 9. 按需规划和重规划

Planner 不再是所有请求的必经节点：

- 简单任务直接调用一个能力。
- 探索任务通过 ReAct 边执行边判断。
- 多交付物、强依赖或长时间任务才先建立计划。

计划步骤应描述子目标，而不是固定函数调用：

```python
class PlanStep(TypedDict):
	"""可根据观察结果调整的子目标。"""

	id: str
	goal: str
	dependencies: list[str]
	candidate_capabilities: list[str]
	status: str
	evidence: list[str]
```

推荐写法：`获得足够的候选岗位`。

不推荐写法：`调用 collect_jobs(keywords=["Agent"], max_pages=3)`。

具体工具和参数应由执行时的上下文决定。

## 10. 确定性策略引擎

模型只负责提出动作，不负责授予权限。所有动作在执行前必须经过独立策略引擎：

```python
def authorize_action(action: AgentAction, context: PolicyContext) -> AuthorizationResult:
	"""在工具执行前进行确定性权限检查。"""

	capability = registry[action["tool"]]

	if action["tool"] in context["forbidden_actions"]:
		return {"decision": "deny", "reason": "用户明确禁止"}

	if capability["side_effect"] == "none":
		return {"decision": "allow"}

	if capability["side_effect"] in {"local_persistent", "external"}:
		return {"decision": "require_approval"}

	return {"decision": "allow"}
```

用户的否定约束必须转换为可执行策略并贯穿整个任务：

```json
{
  "forbidden_actions": [
    "send_greetings",
    "send_hr_reply",
    "start_monitor"
  ],
  "max_side_effect": "none"
}
```

## 11. 统一审批提案

所有敏感能力统一生成 `ActionProposal`，避免每种操作分别维护审批协议。

```python
class ActionProposal(TypedDict):
	"""等待用户确认的动作提案。"""

	id: str
	capability: str
	title: str
	reason: str
	arguments: dict[str, Any]
	preview: dict[str, Any]
	affected_objects: list[dict[str, str]]
	side_effect: str
	reversible: bool
	expires_at: str
```

用户可以确认、取消、编辑后确认，或者只批准部分对象。审批通过后不再回到开放式 Agent 修改参数，而是进入固定执行器。

固定执行器必须：

- 校验提案未过期且未被消费。
- 重新校验参数和当前业务状态。
- 确认执行对象与用户批准对象完全一致。
- 执行限流、发送窗口和安全检查。
- 验证实际执行结果。
- 持久化幂等键和审计记录。

进程内的 `_consumed_actions` 可作为快速保护，但最终幂等状态应持久化到数据库。

## 12. 完成判断与运行预算

通用 Agent 既要避免过早结束，也要避免无限搜索。完成判断采用模型判断和程序校验相结合的方式。

模型声明：

```json
{
  "type": "finish",
  "completed_criteria": [
    "已获得候选岗位",
    "已完成公司研究"
  ],
  "answer": "..."
}
```

程序至少检查：

- 每个成功标准是否存在对应证据。
- 是否有关键工具执行失败。
- 是否还有待审批动作或待补充信息。
- 是否遗漏用户明确要求。
- 是否超过迭代次数、时间或成本预算。

建议每个任务配置：

- 最大 Agent 迭代次数。
- 最大工具调用次数。
- 单能力超时。
- 整体截止时间。
- 模型 Token 或费用预算。
- 同一工具和相同参数的重复调用限制。

## 13. Agent 状态建议

```python
class AgentState(TypedDict, total=False):
	"""通用 Agent 的可恢复运行状态。"""

	conversation_id: str
	messages: list[dict[str, Any]]

	goal: GoalState
	context_summary: str
	retrieved_memories: list[dict[str, Any]]
	candidate_capabilities: list[str]

	plan: list[PlanStep]
	current_step_id: str | None
	observations: list[Observation]
	artifacts: list[ArtifactReference]

	constraints: PolicyConstraints
	pending_proposal: ActionProposal | None
	pending_question: str | None

	iteration: int
	max_iterations: int
	token_budget: int
	deadline_at: str | None

	status: str
	final_answer: str | None
```

## 14. 推荐模块结构

```text
assistant/
├── agent.py                 # 通用 Agent 循环
├── state.py                 # AgentState、GoalState
├── goal_understanding.py    # 目标、约束和成功标准
├── capability_registry.py   # 能力定义
├── capability_retriever.py  # 动态召回候选能力
├── tool_executor.py         # 统一工具执行
├── policy.py                # 权限与副作用判断
├── proposals.py             # 审批提案
├── completion.py            # 完成度判断
├── context.py               # 会话和业务上下文
├── memory.py                # 长期记忆
├── planner.py               # 按需规划
└── observations.py          # 工具结果标准化
```

现有模块迁移建议：

| 当前模块 | 处理方式 |
| --- | --- |
| `graph.py` | 保留 LangGraph，逐步简化为通用循环节点 |
| `understanding.py` | 改为目标、约束和成功标准理解 |
| `routing.py` | 弱化为自主执行、追问、审批三类控制路由 |
| `capabilities.py` | 升级为完整能力注册表 |
| `react.py` | 扩展为主要 Agent Loop |
| `planning.py` | 保留并改为按需 Planner |
| `policy.py` | 保留并加强副作用与用户约束检查 |
| `service.py` | 保留会话入口、流式回调和恢复机制 |
| `deep_research.py` | 作为可选的只读研究能力 |
| `repository.py` | 保留会话、记忆、制品引用和审计记录 |

## 15. 分阶段迁移

### 阶段一：只读通用 Agent

将普通问答、岗位查询和公司研究合并到一个只读 Agent Loop：

- `job_database`
- `web_search`
- `company_research`
- `resume_read`
- `job_detail_read`

取消这些能力对唯一 `intent_type` 的依赖，现有发送和写入流程保持不变。

### 阶段二：纯生成能力

加入可自主调用的生成能力：

- 岗位匹配分析
- 公司对比
- 求职建议
- 招呼语草稿
- HR 回复草稿
- 研究报告生成

生成结果仅作为草稿，不发送、不投递、不修改业务状态。

### 阶段三：统一 ActionProposal

将以下操作改为统一审批提案：

- 启动岗位采集
- 批量评分并写库
- 修改岗位状态
- 发送招呼语
- 发送 HR 回复
- 启动持续监听

审批后继续使用现有固定执行器和安全检查。

### 阶段四：弱化固定意图路由

`intent_type` 最终只用于：

- UI 标签
- 日志和统计
- 评测分析
- 兼容历史会话

不再由它决定 Agent 能看到哪些只读或生成能力。

## 16. 测试与评测

测试重点从“唯一意图是否分类正确”迁移到完整任务行为。

示例：

```text
用户：分析这些岗位，不要发送。

允许：
- 查询岗位
- 读取简历
- 分析匹配度
- 研究公司
- 生成草稿

禁止：
- 发送招呼语
- 回复 HR
- 投递岗位
- 启动持续监听
```

建议评测指标：

- 能力召回率。
- 首次工具选择准确率。
- 工具参数正确率。
- 平均无效工具调用次数。
- 端到端任务完成率。
- 用户约束遵守率。
- 敏感动作拦截率。
- 无审批外部执行次数，目标必须为 0。
- 不必要追问率。
- 过早结束率。
- 循环和预算超限率。

最重要的安全不变量：

> 无论模型输出什么，只要不存在有效且未消费的审批记录，外部发送、投递、上传和后台任务就不可能执行。

## 17. 推荐的首个实施切片

第一步只改造低风险路径：

1. 建立新的能力元数据和副作用分类。
2. 将 `general_question`、`job_query`、`company_research` 合并到只读通用 Agent Loop。
3. 允许 Agent 自主读取岗位、简历并进行公开网页研究。
4. 保留当前审批节点、发送执行器、限流和人工确认逻辑。
5. 建立端到端场景评测，验证工具选择、约束遵守和任务完成率。

该切片可以在不扩大外部操作权限的前提下，验证通用 Agent 架构是否显著改善模糊问题和多步骤研究任务的体验。

