"""BossHunter 问数语义层。

模型只负责把自然语言映射成白名单指标、维度和过滤条件；SQL 始终由本模块确定性生成。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from bosshunter.ai.credentials import call_anthropic_text


# 列表默认只给模型足够浏览的明细，同时保留可由用户显式请求的安全上限。
DEFAULT_LIST_LIMIT = 20
MAX_LIST_LIMIT = 100


@dataclass(frozen=True)
class Metric:
	"""描述一个可查询指标及其稳定业务口径。"""

	label: str
	description: str
	sql: str
	aliases: tuple[str, ...]
	format: str = "integer"
	dataset: str = "jobs"
	event_type: str | None = None


@dataclass(frozen=True)
class Dimension:
	"""描述一个允许分组或过滤的业务维度。"""

	label: str
	sql: str
	aliases: tuple[str, ...]


@dataclass(frozen=True)
class Attribute:
	"""描述实体明细查询中允许展示、过滤和排序的字段。"""

	label: str
	sql: str
	aliases: tuple[str, ...]
	data_type: str = "string"
	capabilities: tuple[str, ...] = ("select", "filter", "sort")
	description: str = ""


@dataclass(frozen=True)
class DetailEntity:
	"""描述明细查询实体的粒度、来源和可用字段。"""

	label: str
	description: str
	grain: str
	primary_key: str
	from_sql: str
	attributes: dict[str, Attribute]
	default_fields: tuple[str, ...]
	base_filter: str = ""


FORMAL_JOB_FILTER = "j.title <> 'HR主动沟通' AND TRIM(COALESCE(j.title, '')) <> '' AND TRIM(COALESCE(j.jd, '')) <> ''"
SENT_STATUSES = "'sent', 'replied', 'needs_resume', 'resume_sent', 'follow_up_sent'"
REPLIED_STATUSES = "'replied', 'needs_resume', 'resume_sent', 'follow_up_sent'"

METRICS: dict[str, Metric] = {
	"job_count": Metric("岗位数", "符合条件的正式岗位数量", "COUNT(DISTINCT j.id)", ("岗位总数", "职位数", "多少岗位")),
	"average_score": Metric("平均AI评分", "已完成 AI 评分岗位的平均匹配分", "ROUND(AVG(NULLIF(j.score, 0)), 2)", ("平均评分", "平均分"), "decimal"),
	"high_score_job_count": Metric(
		"高分岗位数", "AI 匹配分不低于 80 分的正式岗位数量", "COUNT(DISTINCT CASE WHEN j.score >= 80 THEN j.id END)", ("高分岗位", "80分以上岗位")
	),
	"pending_score_job_count": Metric(
		"待评分岗位数", "当前仍处于 pending 状态的正式岗位数量", "COUNT(DISTINCT CASE WHEN j.status = 'pending' THEN j.id END)", ("待评分数", "没评分的岗位")
	),
	"pending_send_job_count": Metric(
		"待发送岗位数", "已确认或招呼语已就绪但尚未发送的岗位数量", "COUNT(DISTINCT CASE WHEN j.status IN ('ready', 'approved') THEN j.id END)", ("待发送数", "还没发送")
	),
	"sent_job_count": Metric(
		"已发送岗位数", "当前已进入发送或后续阶段的岗位数量", f"COUNT(DISTINCT CASE WHEN j.status IN ({SENT_STATUSES}) THEN j.id END)", ("发送数", "已投递数")
	),
	"replied_job_count": Metric(
		"已回复岗位数", "当前已收到 HR 回复或进入回复后续阶段的岗位数量", f"COUNT(DISTINCT CASE WHEN j.status IN ({REPLIED_STATUSES}) THEN j.id END)", ("回复数", "收到回复数")
	),
	"reply_rate": Metric(
		"累计回复率",
		"当前已发送岗位中，已收到 HR 回复或进入回复后续阶段的岗位占比",
		f"ROUND(100.0 * COUNT(DISTINCT CASE WHEN j.status IN ({REPLIED_STATUSES}) THEN j.id END) / NULLIF(COUNT(DISTINCT CASE WHEN j.status IN ({SENT_STATUSES}) THEN j.id END), 0), 2)",
		("回复率", "HR回复率"),
		"percentage",
	),
	"missing_company_info_job_count": Metric(
		"企业信息缺失岗位数",
		"区县、公司规模、行业或企业背景至少一项为空的正式岗位数量",
		"COUNT(DISTINCT CASE WHEN TRIM(COALESCE(j.district, '')) = '' OR TRIM(COALESCE(j.company_size, '')) = '' OR TRIM(COALESCE(j.company_industry, '')) = '' OR TRIM(COALESCE(j.company_background, '')) = '' THEN j.id END)",
		("缺少企业信息", "企业信息不完整"),
	),
	"collected_job_count": Metric("采集岗位数", "时间范围内新采集的正式岗位数", "COUNT(DISTINCT j.id)", ("采集数", "新采集岗位"), dataset="events", event_type="job_collected"),
	"scored_job_count": Metric("评分岗位数", "时间范围内首次或再次进入 AI 已评分阶段的正式岗位数", "COUNT(DISTINCT j.id)", ("评分完成数",), dataset="events", event_type="ai_scored"),
	"sent_event_job_count": Metric("期间发送岗位数", "时间范围内成功进入招呼已发送阶段的正式岗位数", "COUNT(DISTINCT j.id)", ("期间发送数", "最近发送数"), dataset="events", event_type="greeting_sent"),
	"replied_event_job_count": Metric("期间回复岗位数", "时间范围内首次进入 HR 已回复阶段的正式岗位数", "COUNT(DISTINCT j.id)", ("期间回复数", "最近回复数"), dataset="events", event_type="hr_replied"),
}

DIMENSIONS: dict[str, Dimension] = {
	"city": Dimension("城市", "COALESCE(NULLIF(j.city, ''), '未知')", ("地区", "工作城市")),
	"district": Dimension("区县", "COALESCE(NULLIF(j.district, ''), '未知')", ("区域",)),
	"company": Dimension("公司", "COALESCE(NULLIF(j.company, ''), '未知')", ("企业", "公司名称")),
	"company_industry": Dimension("行业", "COALESCE(NULLIF(j.company_industry, ''), '未知')", ("公司行业",)),
	"company_size": Dimension("公司规模", "COALESCE(NULLIF(j.company_size, ''), '未知')", ("规模",)),
	"experience": Dimension("经验要求", "COALESCE(NULLIF(j.experience, ''), '未知')", ("工作经验", "经验")),
	"status": Dimension("当前状态", "j.status", ("状态", "流程阶段")),
	"score_band": Dimension(
		"评分段",
		"CASE WHEN j.score >= 90 THEN '90分以上' WHEN j.score >= 80 THEN '80-89分' WHEN j.score >= 60 THEN '60-79分' ELSE '60分以下' END",
		("分数段", "评分区间"),
	),
}

JOB_ATTRIBUTES: dict[str, Attribute] = {
	"id": Attribute("岗位ID", "j.id", ("岗位编号",)),
	"title": Attribute("岗位名称", "j.title", ("职位", "职位名称", "岗位")),
	"company": Attribute("公司", "j.company", ("企业", "公司名称")),
	"salary": Attribute("薪资", "j.salary", ("工资",)),
	"city": Attribute("城市", "j.city", ("地区", "工作城市")),
	"district": Attribute("区县", "j.district", ("区域",)),
	"experience": Attribute("经验要求", "j.experience", ("经验", "工作经验")),
	"status": Attribute("当前状态", "j.status", ("状态", "流程阶段")),
	"score": Attribute("AI评分", "j.score", ("评分", "匹配分"), "integer"),
	"quick_score": Attribute("初筛评分", "j.quick_score", ("初筛分",), "integer"),
	"score_reason": Attribute("评分理由", "j.score_reason", ("匹配理由", "评分说明"), description="AI 深度评分给出的匹配理由"),
	"jd": Attribute("岗位描述", "j.jd", ("职位描述", "岗位详情", "JD"), description="岗位完整描述，内容可能较长"),
	"hr_name": Attribute("HR姓名", "j.hr_name", ("招聘人", "招聘者")),
	"hr_title": Attribute("HR职位", "j.hr_title", ("招聘人职位",)),
	"hr_active": Attribute("HR活跃度", "j.hr_active", ("招聘人活跃度", "HR在线状态")),
	"company_size": Attribute("公司规模", "j.company_size", ("企业规模",)),
	"company_industry": Attribute("公司行业", "j.company_industry", ("行业", "企业行业")),
	"company_background": Attribute("公司背景", "j.company_background", ("企业背景",)),
	"registered_capital": Attribute("注册资本", "j.registered_capital", ("公司注册资本",)),
	"greeting": Attribute("招呼语", "j.greeting", ("沟通话术",), description="已生成或已发送的岗位招呼语"),
	"resume_path": Attribute("简历路径", "j.resume_path", ("简历文件",)),
	"conversation_id": Attribute("招聘会话ID", "j.conversation_id", ("Boss会话ID", "聊天会话ID")),
	"error_retry_count": Attribute("发送重试次数", "j.error_retry_count", ("失败重试次数",), "integer"),
	"created_at": Attribute("采集时间", "j.created_at", ("创建时间",), "datetime"),
	"updated_at": Attribute("更新时间", "j.updated_at", ("最近更新",), "datetime"),
	"url": Attribute("岗位链接", "j.url", ("链接",)),
}

JOB_EVENT_ATTRIBUTES: dict[str, Attribute] = {
	"id": Attribute("事件ID", "e.id", ("流程事件编号",), "integer"),
	"job_id": Attribute("岗位ID", "e.job_id", ("岗位编号",)),
	"event_type": Attribute("事件类型", "e.event_type", ("流程动作", "节点事件")),
	"event_time": Attribute("事件时间", "e.event_time", ("发生时间",), "datetime"),
	"source": Attribute("事件来源", "e.source", ("记录来源",)),
	"from_status": Attribute("原状态", "e.from_status", ("变更前状态",)),
	"to_status": Attribute("新状态", "e.to_status", ("变更后状态",)),
	"attributes_json": Attribute("事件扩展属性", "e.attributes_json", ("事件详情",), "json", ("select",)),
	"job_title": Attribute("岗位名称", "j.title", ("职位", "职位名称")),
	"company": Attribute("公司", "j.company", ("企业", "公司名称")),
	"current_status": Attribute("岗位当前状态", "j.status", ("当前流程阶段",)),
}

HISTORY_ATTRIBUTES: dict[str, Attribute] = {
	"id": Attribute("历史记录ID", "h.id", ("操作记录编号",), "integer"),
	"job_id": Attribute("岗位ID", "h.job_id", ("岗位编号",)),
	"action": Attribute("操作类型", "h.action", ("历史动作", "操作动作")),
	"detail": Attribute("操作详情", "h.detail", ("历史详情",), capabilities=("select", "filter")),
	"created_at": Attribute("操作时间", "h.created_at", ("发生时间",), "datetime"),
	"job_title": Attribute("岗位名称", "j.title", ("职位", "职位名称")),
	"company": Attribute("公司", "j.company", ("企业", "公司名称")),
	"current_status": Attribute("岗位当前状态", "j.status", ("当前流程阶段",)),
}

# 保留原常量名，兼容现有聚合查询和外部导入。
ATTRIBUTES = JOB_ATTRIBUTES

DETAIL_ENTITIES: dict[str, DetailEntity] = {
	"job": DetailEntity(
		"岗位",
		"一行代表一个正式岗位的当前快照",
		"每个岗位一行",
		"id",
		"jobs j",
		JOB_ATTRIBUTES,
		("id", "title", "company", "salary", "city", "score", "status", "created_at"),
		FORMAL_JOB_FILTER,
	),
	"job_event": DetailEntity(
		"岗位流程事件",
		"一行代表岗位流程中的一次状态事件，可用于查看阶段流转明细",
		"每次岗位流程事件一行",
		"id",
		"job_events e JOIN jobs j ON j.id = e.job_id",
		JOB_EVENT_ATTRIBUTES,
		("id", "job_id", "job_title", "company", "event_type", "event_time", "from_status", "to_status"),
		FORMAL_JOB_FILTER,
	),
	"job_history": DetailEntity(
		"岗位操作历史",
		"一行代表应用记录的一次岗位操作或处理结果",
		"每次岗位操作一行",
		"id",
		"history h JOIN jobs j ON j.id = h.job_id",
		HISTORY_ATTRIBUTES,
		("id", "job_id", "job_title", "company", "action", "detail", "created_at"),
		FORMAL_JOB_FILTER,
	),
}

ENTITY_RELATIONSHIPS = [
	{"from": "job_event.job_id", "to": "job.id", "cardinality": "many_to_one", "description": "多个流程事件归属于一个岗位"},
	{"from": "job_history.job_id", "to": "job.id", "cardinality": "many_to_one", "description": "多条操作历史归属于一个岗位"},
]

STATUS_VALUES = {
	"pending": "待初筛或待评分",
	"filtered": "初筛未通过",
	"scored": "已完成深度评分",
	"ready": "招呼语已生成，待人工确认",
	"approved": "人工已确认，待发送",
	"rejected": "人工已拒绝",
	"sent": "招呼语已发送",
	"replied": "HR 已回复",
	"needs_resume": "HR 索要简历，等待人工发送",
	"resume_sent": "简历已发送",
	"follow_up_sent": "跟进消息已发送",
	"error": "发送失败",
}

EVENT_TYPE_VALUES = {
	"job_collected": "岗位已采集",
	"prefilter_rejected": "初筛未通过",
	"ai_scored": "AI 评分完成",
	"greeting_generated": "招呼语生成完成",
	"user_approved": "人工确认通过",
	"user_rejected": "人工拒绝",
	"greeting_sent": "招呼语发送成功",
	"hr_replied": "HR 首次回复",
	"resume_requested": "HR 索要简历",
	"resume_sent": "简历已发送",
	"follow_up_sent": "跟进消息已发送",
	"send_failed": "发送失败",
	"status_changed": "其他状态变化",
}

CONCEPTS: dict[str, dict[str, Any]] = {
	"formal_job": {
		"label": "正式岗位",
		"description": "职位名称和 JD 完整，且不是 HR 主动沟通占位记录",
		"aliases": ["正式职位"],
	},
	"unscored_job": {
		"label": "未评分岗位",
		"description": "当前状态为 pending、尚未完成 AI 评分的正式岗位",
		"aliases": ["没评分的岗位", "待评分岗位", "还没有评分的岗位"],
		"filter": {"field": "status", "operator": "eq", "value": "pending"},
	},
}

DEFAULT_LIST_FIELDS = ["id", "title", "company", "salary", "city", "score", "status", "created_at"]

# 时间预设统一在编译期展开为数据库端时钟表达式，避免应用侧时区与数据库
# CURRENT_TIMESTAMP（无时区列）不一致导致窗口整体偏移。
TIME_PRESETS: dict[str, int | None] = {"last_7_days": 7, "last_30_days": 30, "last_90_days": 90, "today": None}

FILTER_OPERATORS = {"eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "contains": "LIKE"}
FILTER_FIELDS = {key: value.sql for key, value in ATTRIBUTES.items()}


def semantic_catalog() -> dict[str, Any]:
	"""返回可供模型规划和前端展示的语义目录。"""
	return {
		"metrics": {
			name: {"label": item.label, "description": item.description, "aliases": list(item.aliases), "format": item.format}
			for name, item in METRICS.items()
		},
		"dimensions": {
			name: {"label": item.label, "aliases": list(item.aliases)} for name, item in DIMENSIONS.items()
		},
		"entities": {
			name: {
				"label": entity.label,
				"description": entity.description,
				"grain": entity.grain,
				"primary_key": entity.primary_key,
				"attributes": {
					name: {
						"label": item.label,
						"aliases": list(item.aliases),
						"data_type": item.data_type,
						"capabilities": list(item.capabilities),
						"description": item.description,
					}
					for name, item in entity.attributes.items()
				},
				"default_fields": list(entity.default_fields),
			}
			for name, entity in DETAIL_ENTITIES.items()
		},
		"relationships": ENTITY_RELATIONSHIPS,
		"value_dictionaries": {"job.status": STATUS_VALUES, "job_event.event_type": EVENT_TYPE_VALUES},
		"concepts": CONCEPTS,
		"query_kinds": ["aggregate", "list"],
		"default_filters": ["仅统计职位和 JD 均完整的正式岗位"],
		"default_time_field": "jobs.created_at（采集时间）",
	}


def looks_like_semantic_question(question: str) -> bool:
	"""判断问题是否属于可尝试语义规划的只读问数。"""
	return bool(question.strip())


def _extract_json_object(text: str) -> dict[str, Any] | None:
	"""从模型回复中提取一个 JSON 对象。"""
	match = re.search(r"\{.*\}", text, re.DOTALL)
	if not match:
		return None
	try:
		value = json.loads(match.group(0))
	except (TypeError, ValueError):
		return None
	return value if isinstance(value, dict) else None


def plan_semantic_query(question: str, config: dict[str, Any], goal: str = "", feedback: str = "") -> dict[str, Any]:
	"""使用模型把问题映射为受控语义查询计划。"""
	goal_block = f"用户多轮合并后的目标（补充上文指代，与问题重复时以问题为准）：{goal}" if goal.strip() else "无"
	feedback_block = f"前序执行观察：{feedback}" if feedback.strip() else "无，这是第一次尝试。"
	prompt = f"""你是 BossHunter 求职数据分析规划器。把用户问题转换成语义查询计划。

只能使用目录内已有的 metric、dimensions 和 filter field，不要生成 SQL。
先判断 query_kind：
- aggregate：数量、平均、占比、分组、趋势等聚合问数；必须提供 metric。
- list：哪些、列出、查看一批符合条件的岗位；提供 fields，不要提供 metric。
- unsupported：目录无法完整表达问题，不得选择近似指标。

输出 JSON 示例：
{{"query_kind":"aggregate","metric":"job_count","dimensions":[],"filters":[],"time_range":null,"order":"desc","limit":20}}
{{"query_kind":"list","entity":"job","fields":["id","title","company","score","status"],"filters":[{{"field":"status","operator":"eq","value":"pending"}}],"order_by":[{{"field":"created_at","direction":"desc"}}],"limit":20}}
{{"query_kind":"unsupported","reason":"语义目录缺少所需字段"}}
filters 每项格式：{{"field":"city","operator":"eq","value":"上海"}}。
time_range 格式：{{"preset":"today"}}、{{"preset":"last_7_days"}}、{{"preset":"last_30_days"}}、{{"preset":"last_90_days"}} 或 null，时间默认按岗位采集时间。
list 查询必须先选择与问题粒度一致的 entity：岗位当前信息用 job，流程事件用 job_event，操作日志用 job_history。
字段的 capabilities 决定是否允许 select、filter、sort；不得选择目录外字段。list 默认返回 20 条明细，用户明确要求更多时才提高 limit。
“未评分、没评分、待评分、还没有评分”统一使用 job.status = pending。
没有明确分组时 dimensions 为空；最多两个维度；limit 范围 1-100。不要生成 SQL。

语义目录：
{json.dumps(semantic_catalog(), ensure_ascii=False)}

用户问题：{question}

{goal_block}

{feedback_block}
"""
	try:
		answer = call_anthropic_text(prompt, config) or ""
	except Exception as exc:
		# 语义规划不可用时由上层降级到受控只读 SQL，不能让普通问数整体失败。
		raise ValueError("语义规划服务不可用") from exc
	plan = _extract_json_object(answer)
	if not plan:
		raise ValueError("模型没有返回有效的语义查询计划")
	return plan


def _validate_filters(filters: Any, attributes: dict[str, Attribute] | None = None) -> list[dict[str, Any]]:
	"""校验语义过滤条件仅引用白名单属性。"""
	allowed_attributes = attributes or ATTRIBUTES
	if not isinstance(filters, list):
		raise ValueError("过滤条件必须是数组")
	for item in filters:
		if not isinstance(item, dict) or item.get("field") not in allowed_attributes:
			raise ValueError("过滤字段不在语义层白名单中")
		if "filter" not in allowed_attributes[str(item["field"])].capabilities:
			raise ValueError("字段不允许用于过滤")
		if item.get("operator") not in {*FILTER_OPERATORS, "in"}:
			raise ValueError("过滤操作符不受支持")
	return filters


def _validate_plan(plan: dict[str, Any]) -> tuple[str, list[str], list[dict[str, Any]], dict[str, Any] | None, str, int]:
	"""验证计划中的所有标识符和参数均属于白名单。"""
	query_kind = str(plan.get("query_kind") or "aggregate")
	if query_kind == "unsupported":
		raise ValueError(str(plan.get("reason") or "问题超出语义目录能力"))
	if query_kind != "aggregate":
		raise ValueError(f"聚合编译器不支持查询类型：{query_kind}")
	metric_name = str(plan.get("metric", ""))
	if metric_name not in METRICS:
		raise ValueError(f"不支持的指标：{metric_name}")
	dimensions = plan.get("dimensions", [])
	if not isinstance(dimensions, list) or len(dimensions) > 2 or any(item not in DIMENSIONS for item in dimensions):
		raise ValueError("维度无效或超过两个")
	filters = _validate_filters(plan.get("filters", []))
	time_range = plan.get("time_range")
	if time_range is not None and (not isinstance(time_range, dict) or time_range.get("preset") not in TIME_PRESETS):
		raise ValueError("时间范围不受支持")
	order = str(plan.get("order", "desc")).lower()
	if order not in {"asc", "desc"}:
		raise ValueError("排序方向无效")
	limit = int(plan.get("limit", 20))
	if not 1 <= limit <= 100:
		raise ValueError("limit 必须在 1 到 100 之间")
	return metric_name, dimensions, filters, time_range, order, limit


def _append_filters(
	where_parts: list[str],
	params: list[Any],
	filters: list[dict[str, Any]],
	attributes: dict[str, Attribute] | None = None,
) -> None:
	"""把已校验的过滤条件编译为参数化 WHERE 片段。"""
	allowed_attributes = attributes or ATTRIBUTES
	for item in filters:
		field_sql = allowed_attributes[str(item["field"])].sql
		operator = str(item["operator"])
		value = item.get("value")
		if operator == "in":
			if not isinstance(value, list) or not value:
				raise ValueError("in 过滤必须提供非空数组")
			where_parts.append(f"{field_sql} IN ({', '.join('?' for _ in value)})")
			params.extend(value)
		else:
			where_parts.append(f"{field_sql} {FILTER_OPERATORS[operator]} ?")
			params.append(f"%{value}%" if operator == "contains" else value)


def compile_list_query(plan: dict[str, Any]) -> tuple[str, list[Any], dict[str, Any]]:
	"""将业务实体明细计划编译为参数化只读 SQL。"""
	entity_name = str(plan.get("entity", "job"))
	if str(plan.get("query_kind")) != "list" or entity_name not in DETAIL_ENTITIES:
		raise ValueError("明细查询实体不受支持")
	entity = DETAIL_ENTITIES[entity_name]
	attributes = entity.attributes
	fields = plan.get("fields") or list(entity.default_fields)
	if (
		not isinstance(fields, list)
		or not fields
		or any(field not in attributes or "select" not in attributes[field].capabilities for field in fields)
	):
		raise ValueError("明细字段不在语义层白名单中")
	filters = _validate_filters(plan.get("filters", []), attributes)
	limit = int(plan.get("limit", DEFAULT_LIST_LIMIT))
	if not 1 <= limit <= MAX_LIST_LIMIT:
		raise ValueError(f"limit 必须在 1 到 {MAX_LIST_LIMIT} 之间")
	order_by = plan.get("order_by", [])
	if not isinstance(order_by, list) or len(order_by) > 2:
		raise ValueError("排序条件无效或超过两个")
	for item in order_by:
		if (
			not isinstance(item, dict)
			or item.get("field") not in attributes
			or "sort" not in attributes[str(item.get("field"))].capabilities
			or str(item.get("direction", "asc")).lower() not in {"asc", "desc"}
		):
			raise ValueError("排序字段或方向不受支持")
	where_parts = [entity.base_filter] if entity.base_filter else []
	params: list[Any] = []
	_append_filters(where_parts, params, filters, attributes)
	select_sql = ", ".join(f"{attributes[field].sql} AS {field}" for field in fields)
	# 窗口总数在 LIMIT 前计算，既保护上下文规模，也避免把返回条数误报成真实总数。
	select_sql += ", COUNT(*) OVER() AS __total_count"
	sql = f"SELECT {select_sql} FROM {entity.from_sql}"
	if where_parts:
		sql += f" WHERE {' AND '.join(where_parts)}"
	if order_by:
		sql += " ORDER BY " + ", ".join(
			f"{attributes[str(item['field'])].sql} {str(item.get('direction', 'asc')).upper()}" for item in order_by
		)
	sql += " LIMIT ?"
	params.append(limit)
	return sql, params, {
		"query_kind": "list",
		"entity": entity_name,
		"entity_label": entity.label,
		"grain": entity.grain,
		"primary_key": entity.primary_key,
		"fields": fields,
		"limit": limit,
		"default_filters": ["仅统计职位和 JD 均完整的正式岗位"] if entity.base_filter else [],
	}


def compile_semantic_query(plan: dict[str, Any]) -> tuple[str, list[Any], dict[str, Any]]:
	"""将已验证的语义计划编译为参数化、只读 SQL。"""
	metric_name, dimensions, filters, time_range, order, limit = _validate_plan(plan)
	metric = METRICS[metric_name]
	select_parts = [f"{DIMENSIONS[name].sql} AS {name}" for name in dimensions]
	select_parts.append(f"{metric.sql} AS {metric_name}")
	where_parts = [FORMAL_JOB_FILTER]
	from_sql = "jobs j"
	time_field = "j.created_at"
	if metric.dataset == "events":
		from_sql = "job_events e JOIN jobs j ON j.id = e.job_id"
		time_field = "e.event_time"
		where_parts.append("e.event_type = ?")
	params: list[Any] = []
	if metric.event_type:
		params.append(metric.event_type)
	_append_filters(where_parts, params, filters)
	if time_range:
		preset = str(time_range["preset"])
		days = TIME_PRESETS[preset]
		# 使用数据库端时钟表达式，避免应用与数据库时区不一致导致窗口偏移。
		clock_sql = "date('now')" if days is None else f"datetime('now', '-{days} days')"
		where_parts.append(f"{time_field} >= {clock_sql}")
	sql = f"SELECT {', '.join(select_parts)} FROM {from_sql} WHERE {' AND '.join(where_parts)}"
	if dimensions:
		dimension_sql = ", ".join(DIMENSIONS[name].sql for name in dimensions)
		sql += f" GROUP BY {dimension_sql} ORDER BY {metric_name} {order.upper()} LIMIT ?"
		params.append(limit)
	metadata = {
		"metric": metric_name,
		"metric_label": metric.label,
		"definition": metric.description,
		"format": metric.format,
		"dimensions": dimensions,
		"default_filters": ["仅统计职位和 JD 均完整的正式岗位"],
		"time_field": time_field.removeprefix("j.").removeprefix("e."),
	}
	return sql, params, metadata


def query_semantic_database(conn: Any, question: str, config: dict[str, Any], goal: str = "") -> dict[str, Any]:
	"""规划并执行语义问数查询，并返回指标口径和查询血缘。

	list 查询返回 0 行时追加观察重新规划一次，与 SQL 路径的多轮修正体验对齐；
	重试失败（如规划器改判 unsupported）时保留首轮空结果，不中断问数。
	"""
	attempts: list[dict[str, Any]] = []
	rows: list[Any] = []
	payload: dict[str, Any] = {}
	feedback = ""
	for attempt_number in (1, 2):
		try:
			plan = plan_semantic_query(question, config, goal, feedback)
			query_kind = str(plan.get("query_kind") or "aggregate")
			if query_kind == "list":
				sql, params, metadata = compile_list_query(plan)
			else:
				sql, params, metadata = compile_semantic_query(plan)
			rows = conn.execute(sql, params).fetchall()
		except ValueError:
			# 首轮失败（无法映射、白名单校验不通过）仍按原语义抛给上层降级；
			# 重试轮失败则保留首轮空结果。
			if not payload:
				raise
			break
		attempts.append(
			{
				"attempt": attempt_number,
				"sql": sql,
				"params": params,
				"status": "completed" if rows else "empty",
			}
		)
		items = [dict(row) for row in rows]
		total_count = int(items[0].get("__total_count", len(items))) if items else 0
		for item in items:
			item.pop("__total_count", None)
		returned_count = len(items)
		payload = {
			"mode": "semantic",
			"semantic_plan": plan,
			"semantic": metadata,
			"rows": items,
			"row_count": total_count if query_kind == "list" else returned_count,
		}
		if query_kind == "list":
			limit = int(metadata.get("limit", returned_count))
			payload.update({
				"total_count": total_count,
				"returned_count": returned_count,
				"limit": limit,
				"has_more": total_count > returned_count,
			})
		if query_kind == "aggregate":
			payload["metric"] = metadata
		if rows or query_kind != "list" or attempt_number == 2:
			break
		feedback = "第一次 list 查询返回 0 行记录，请放宽过滤条件或修正状态、实体取值后重新规划。"
	payload["query"] = {"sql": sql, "params": params, "attempts": attempts}
	return payload


__all__ = [
	"DIMENSIONS",
	"ATTRIBUTES",
	"DETAIL_ENTITIES",
	"METRICS",
	"compile_list_query",
	"compile_semantic_query",
	"looks_like_semantic_question",
	"plan_semantic_query",
	"query_semantic_database",
	"semantic_catalog",
]
