"""根据问题的证据需求隔离问数、私有资料和联网研究能力。"""

from __future__ import annotations

from bosshunter.assistant.models import EvidenceDomain, EvidenceRoute, GoalState, IntentResult


_ANALYTICS_TERMS = (
	"多少", "几个", "数量", "占比", "比例", "趋势", "排名", "排行", "漏斗", "转化率", "平均",
	"已发送", "已回复", "待评分", "未评分", "采集了", "数据库", "统计",
)
_PRIVATE_TERMS = (
	"我的简历", "简历", "我的经历", "我的背景", "我的优势", "我的项目", "适合我", "匹配我", "匹配度",
	"岗位要求", "职位描述", "jd", "记忆", "偏好",
)
_WEB_TERMS = (
	"联网", "上网", "搜索网页", "最新", "最近新闻", "新闻", "融资", "裁员", "经营情况", "创始人",
	"老板", "主营业务", "旗下业务", "公司怎么样", "背调", "调查公司", "官网",
)
_FRESHNESS_TERMS = ("最新", "最近", "目前", "现在", "今年", "近期", "新闻")


def route_evidence(question: str, intent: IntentResult, goal: GoalState | None = None) -> EvidenceRoute:
	"""确定本轮问题需要访问的证据域。

	Args:
		question: 用户当前问题。
		intent: 已经过策略和槽位校验的意图。
		goal: 可选的通用目标状态。

	Returns:
		稳定的证据域路由；副作用能力不在这里授权。
	"""
	text = " ".join((question, intent.get("goal", ""), goal.get("goal", "") if goal else "")).lower()
	domains: list[EvidenceDomain] = []
	intent_type = intent["intent_type"]
	definition_question = any(term in question.lower() for term in ("什么是", "是什么意思", "解释一下", "概念"))
	requests_business_data = intent_type == "job_query" or any(
		term in question.lower() for term in ("当前", "最近采集", "我的岗位", "数据库", "统计", "多少", "几个")
	)

	if (not definition_question or requests_business_data) and (
		intent_type == "job_query" or any(term in text for term in _ANALYTICS_TERMS)
	):
		domains.append("structured_data")
	if any(term in text for term in _PRIVATE_TERMS):
		domains.append("private_document")
	if intent_type == "company_research" or any(term in text for term in _WEB_TERMS):
		domains.append("public_current")

	# 普通公司单点事实已经被理解层降级为 general_question，但仍必须使用公开资料。
	if intent_type == "general_question" and any(term in text for term in ("公司", "业务", "产品", "创始人")):
		if any(term in text for term in _WEB_TERMS) and "public_current" not in domains:
			domains.append("public_current")

	domains = list(dict.fromkeys(domains))
	if not domains:
		mode: EvidenceDomain = "direct"
	elif len(domains) == 1:
		mode = domains[0]
	else:
		mode = "mixed"
	return {
		"mode": mode,
		"domains": domains,
		"reason": _route_reason(domains),
		"freshness_required": any(term in text for term in _FRESHNESS_TERMS),
	}


def allowed_tool_names(route: EvidenceRoute, web_enabled: bool) -> set[str]:
	"""把证据域转换成当前允许的只读工具集合。

	Args:
		route: 已确定的证据域。
		web_enabled: 用户是否启用联网能力。

	Returns:
		仅包含对应领域的只读工具名。
	"""
	tools: set[str] = set()
	for domain in route["domains"]:
		if domain == "structured_data":
			tools.add("job_database")
		elif domain == "private_document":
			tools.add("resume_read")
		elif domain == "public_current" and web_enabled:
			tools.update({"web_search", "company_research"})
	return tools


def _route_reason(domains: list[EvidenceDomain]) -> str:
	"""生成便于审计和前端展示的简短路由理由。"""
	labels = {
		"structured_data": "需要结构化业务数据",
		"private_document": "需要用户私有资料",
		"public_current": "需要公开时效信息",
	}
	return "；".join(labels[domain] for domain in domains) if domains else "现有对话知识足以直接回答"


__all__ = ["allowed_tool_names", "route_evidence"]
