"""根据目标、上下文和策略动态召回能力。"""

from bosshunter.assistant.goal_understanding import goal_search_text
from bosshunter.assistant.models import CapabilityMatch, CapabilitySpec, GoalState


_TAG_TERMS: dict[str, tuple[str, ...]] = {
	"jobs": ("岗位", "职位", "job", "招聘"),
	"database": ("本地", "数据库", "已有", "当前", "最近", "多少"),
	"resume": ("简历", "经历", "技能", "背景", "匹配"),
	"company": ("公司", "企业", "雇主", "业务", "值得去", "背调", "对比"),
	"research": ("研究", "公开信息", "资料", "搜索", "调查", "对比"),
	"send": ("发送", "投递", "招呼", "回复"),
	"monitor": ("监听", "监测", "持续"),
}


def retrieve_capabilities(
	goal: GoalState,
	registry: dict[str, CapabilitySpec],
	*,
	web_enabled: bool,
	limit: int = 8,
	allow_side_effects: bool = False,
) -> list[CapabilityMatch]:
	"""召回与目标相关且未被用户策略禁止的能力。

	Args:
		goal: 通用目标状态。
		registry: 完整能力注册表。
		web_enabled: 当前会话是否允许联网。
		limit: 最多返回的候选数。
		allow_side_effects: 是否允许召回有副作用能力；首个迁移切片默认关闭。

	Returns:
		按相关度排序的能力匹配结果。
	"""
	text = goal_search_text(goal)
	forbidden = " ".join(goal["constraints"])
	ranked: list[tuple[int, str, CapabilitySpec]] = []
	for name, spec in registry.items():
		if not allow_side_effects and spec["side_effect"] != "none":
			continue
		if _is_forbidden(name, spec, forbidden):
			continue
		if "web" in spec["tags"] and not web_enabled:
			continue
		score = _relevance_score(text, spec)
		if score > 0:
			ranked.append((score, name, spec))
	ranked.sort(key=lambda item: (-item[0], item[1]))
	return [_to_match(name, spec, goal) for _, name, spec in ranked[:limit]]


def _relevance_score(text: str, spec: CapabilitySpec) -> int:
	"""使用可审计的标签和描述关键词计算召回分数。"""
	score = 0
	for tag in spec["tags"]:
		if any(term in text for term in _TAG_TERMS.get(tag, (tag,))):
			score += 3
	for phrase in spec["when_to_use"]:
		if phrase.lower() in text:
			score += 2
	if spec["name"].replace("_", " ") in text:
		score += 2
	return score


def _is_forbidden(name: str, spec: CapabilitySpec, constraints: str) -> bool:
	"""执行用户否定约束的确定性过滤。"""
	if name in constraints or str(spec["tool_name"]) in constraints:
		return True
	return "禁止发送或投递" in constraints and spec["side_effect"] == "external"


def _to_match(name: str, spec: CapabilitySpec, goal: GoalState) -> CapabilityMatch:
	"""把注册元数据转换为现有图可消费的匹配结果。"""
	arguments = dict(goal["entities"])
	if spec["tool_name"] == "web_search" and "query" not in arguments:
		arguments = {"query": goal["user_request"]}
	return {
		"capability": name,
		"tool_name": spec["tool_name"],
		"arguments": arguments,
		"risk_level": spec["risk_level"],
		"read_only": spec["side_effect"] == "none",
		"requires_approval": spec["requires_approval"],
	}


__all__ = ["retrieve_capabilities"]
