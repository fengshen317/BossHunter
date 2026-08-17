"""上下文解析、目标提取和歧义检查。"""

import json
import re
from collections.abc import Callable
from typing import Any, cast, get_args

from bosshunter.assistant.models import (
	ContextSnapshot,
	InteractionMode,
	IntentAction,
	IntentResult,
	IntentStep,
	IntentType,
	RiskLevel,
)
from bosshunter.assistant.policy import apply_intent_policy


_INTENT_TYPES = set(get_args(IntentType))
_INTENT_ACTIONS = set(get_args(IntentAction))
_INTERACTION_MODES = set(get_args(InteractionMode))
_RISK_LEVELS = set(get_args(RiskLevel))


def _default_mode(intent_type: IntentType) -> InteractionMode:
	"""根据主意图生成兼容旧模型输出的交互模式。"""
	if intent_type == "general_question":
		return "answer"
	if intent_type == "reply_draft":
		return "draft"
	return "execute"


def _normalize_steps(payload: Any) -> list[IntentStep]:
	"""把模型步骤清洗为字段完整且依赖可追踪的任务步骤。"""
	if not isinstance(payload, list):
		return []
	steps: list[IntentStep] = []
	used_ids: set[str] = set()
	for index, raw_step in enumerate(payload, start=1):
		if not isinstance(raw_step, dict) or raw_step.get("action") not in _INTENT_ACTIONS:
			continue
		step_id = str(raw_step.get("id") or f"step_{index}").strip() or f"step_{index}"
		if step_id in used_ids:
			step_id = f"step_{index}"
		used_ids.add(step_id)
		arguments = raw_step.get("arguments") if isinstance(raw_step.get("arguments"), dict) else {}
		dependencies = raw_step.get("depends_on") if isinstance(raw_step.get("depends_on"), list) else []
		steps.append(
			{
				"id": step_id,
				"action": cast(IntentAction, raw_step["action"]),
				"arguments": arguments,
				"depends_on": [str(item) for item in dependencies if str(item).strip()],
				"enabled": raw_step.get("enabled") is not False,
				"reason": str(raw_step.get("reason", "")).strip(),
			}
		)
	return steps


def resolve_context(state: dict[str, Any]) -> ContextSnapshot:
	"""从 LangGraph 状态中提取本轮理解所需的可信上下文。

	Args:
		state: 当前 LangGraph 状态。

	Returns:
		只包含最近消息、当前岗位和联网开关的上下文快照。
	"""
	messages = list(state.get("messages", []))[-12:]
	user_messages = [item for item in messages if item.get("role") == "user"]
	question = str(user_messages[-1].get("content", "")).strip() if user_messages else ""
	previous_messages = messages[:-1] if messages and messages[-1].get("role") == "user" else messages
	last_assistant = next((item for item in reversed(previous_messages) if item.get("role") == "assistant"), None)
	previous_intent = state.get("intent") if isinstance(state.get("intent"), dict) else None
	pending_intent = (
		cast(IntentResult, previous_intent)
		if last_assistant
		and last_assistant.get("message_type") == "clarification"
		and previous_intent
		and previous_intent.get("missing_fields")
		else None
	)
	return {
		"question": question,
		"recent_messages": messages,
		"current_job_id": str(state["job_id"]) if state.get("job_id") else None,
		"web_enabled": bool(state.get("web_enabled", False)),
		"pending_intent": pending_intent,
	}


def _looks_like_slot_answer(question: str) -> bool:
	"""判断当前短消息是否像对上一轮澄清问题的直接回答。

	Args:
		question: 当前用户消息。

	Returns:
		消息简短且不包含明显的新任务表达时返回 True。
	"""
	normalized = question.strip()
	if not normalized or len(normalized) > 100:
		return False
	new_request_signals = (
		"帮我",
		"请问",
		"怎么",
		"为什么",
		"搜索",
		"采集",
		"评分",
		"生成",
		"发送",
		"查询",
		"分析",
		"？",
		"?",
	)
	return not any(signal in normalized for signal in new_request_signals)


def _fill_pending_intent(context: ContextSnapshot, intent: IntentResult) -> IntentResult:
	"""使用当前短回复补全上一轮明确缺失的单个槽位。

	Args:
		context: 包含上一轮待补全意图的上下文。
		intent: 本轮模型识别结果。

	Returns:
		补全成功时返回延续后的意图，否则原样返回本轮意图。
	"""
	pending = context.get("pending_intent")
	question = context["question"].strip(" ，,。\"'“”")
	if not pending or len(pending["missing_fields"]) != 1 or not _looks_like_slot_answer(question):
		return intent
	field = pending["missing_fields"][0]
	value: Any = None
	if field == "company":
		value = question
	elif field == "keywords":
		value = [item.strip() for item in re.split(r"[，,、]", question) if item.strip()]
	elif field == "message":
		value = question
	if not value:
		return intent

	entities = {**pending["entities"], field: value}
	steps: list[IntentStep] = []
	for step in pending.get("steps", []):
		steps.append({**step, "arguments": {**step["arguments"], field: value}})
	return {
		**pending,
		"goal": pending["goal"],
		"entities": entities,
		"missing_fields": [],
		"confidence": max(pending["confidence"], 0.9),
		"reason": f"用户补充了上一轮缺失字段：{field}。",
		"steps": steps,
	}


def _extract_json_object(text: str) -> dict[str, Any] | None:
	"""从模型文本中提取首个 JSON 对象，兼容 Markdown 代码块。

	Args:
		text: 模型原始文本。

	Returns:
		解析成功的字典；解析失败时返回 None。
	"""
	start = text.find("{")
	end = text.rfind("}")
	if start < 0 or end <= start:
		return None
	try:
		payload = json.loads(text[start : end + 1])
	except (json.JSONDecodeError, TypeError):
		return None
	return payload if isinstance(payload, dict) else None


def _keywords_from_question(question: str) -> list[str]:
	"""从明确的岗位搜索表达中提取关键词。

	Args:
		question: 用户问题。

	Returns:
		最多一个未经猜测的搜索关键词。
	"""
	match = re.search(r"(?:采集|搜索|找|查找)(?:一下|一些|一批|帮我)?(.+?)(?:岗位|职位)", question, re.IGNORECASE)
	keyword = match.group(1).strip(" ，,。的") if match else ""
	return [keyword] if keyword else []


def fallback_intent(question: str) -> IntentResult:
	"""在模型输出不可用时用确定性规则生成保守意图。

	Args:
		question: 用户问题。

	Returns:
		不会越权提升置信度或降低风险的结构化意图。
	"""
	normalized = question.replace(" ", "").lower()
	intent_type: IntentType = "general_question"
	risk_level: RiskLevel = "low"
	entities: dict[str, Any] = {}
	confidence = 0.72

	if any(term in normalized for term in ("完整流程", "全流程", "一键执行", "runpipeline")):
		intent_type, risk_level, confidence = "pipeline_run", "high", 0.95
	elif any(term in normalized for term in ("发送招呼语", "投递", "打招呼")):
		intent_type, risk_level, confidence = "greeting_send", "high", 0.9
	elif any(term in normalized for term in ("发送回复", "回复hr", "回复人事")):
		intent_type, risk_level, confidence = "reply_send", "high", 0.9
	elif any(term in normalized for term in ("监测回复", "监听回复", "监控hr", "monitor")):
		intent_type, risk_level, confidence = "reply_monitor", "medium", 0.9
	elif any(term in normalized for term in ("生成招呼语", "写招呼语")):
		intent_type, risk_level, confidence = "greeting_generate", "medium", 0.9
	elif any(term in normalized for term in ("评分", "打分", "score")):
		intent_type, risk_level, confidence = "job_score", "medium", 0.88
	elif any(term in normalized for term in ("公司背调", "调查公司", "了解公司")):
		intent_type, confidence = "company_research", 0.85
	elif any(term in normalized for term in ("怎么回复", "回复话术", "怎么回hr")):
		intent_type, risk_level, confidence = "reply_draft", "medium", 0.88
	elif any(verb in normalized for verb in ("采集", "搜索", "找", "查找")) and any(
		noun in normalized for noun in ("岗位", "职位")
	):
		intent_type, risk_level, confidence = "job_search", "medium", 0.88
		entities["keywords"] = _keywords_from_question(question)
	elif any(term in normalized for term in ("有哪些岗位", "岗位数据", "岗位统计", "查询岗位")):
		intent_type, confidence = "job_query", 0.85

	return {
		"goal": question,
		"intent_type": intent_type,
		"entities": entities,
		"constraints": {},
		"missing_fields": [],
		"confidence": confidence,
		"risk_level": risk_level,
		"reason": "模型结果不可用，使用保守规则识别。",
		"mode": _default_mode(intent_type),
		"steps": (
			[
				{
					"id": "step_1",
					"action": cast(IntentAction, intent_type),
					"arguments": dict(entities),
					"depends_on": [],
					"enabled": True,
					"reason": "由保守规则识别。",
				}
			]
			if intent_type in _INTENT_ACTIONS
			else []
		),
	}


def normalize_intent(payload: dict[str, Any] | None, question: str) -> IntentResult:
	"""校验并归一化模型返回的意图数据。

	Args:
		payload: 模型返回的 JSON 字典。
		question: 当前用户问题。

	Returns:
		字段完整、枚举合法且置信度位于 0 到 1 的意图结果。
	"""
	if not payload or payload.get("intent_type") not in _INTENT_TYPES:
		return fallback_intent(question)
	try:
		confidence = max(0.0, min(float(payload.get("confidence", 0.0)), 1.0))
	except (TypeError, ValueError):
		confidence = 0.0
	risk = payload.get("risk_level", "medium")
	risk_level: RiskLevel = risk if risk in _RISK_LEVELS else "medium"
	entities = payload.get("entities") if isinstance(payload.get("entities"), dict) else {}
	constraints = payload.get("constraints") if isinstance(payload.get("constraints"), dict) else {}
	missing = payload.get("missing_fields") if isinstance(payload.get("missing_fields"), list) else []
	intent_type = cast(IntentType, payload["intent_type"])
	mode_value = payload.get("mode")
	mode = cast(InteractionMode, mode_value) if mode_value in _INTERACTION_MODES else _default_mode(intent_type)
	return {
		"goal": str(payload.get("goal") or question).strip(),
		"intent_type": intent_type,
		"entities": entities,
		"constraints": constraints,
		"missing_fields": [str(item) for item in missing if str(item).strip()],
		"confidence": confidence,
		"risk_level": risk_level,
		"reason": str(payload.get("reason", "")).strip(),
		"mode": mode,
		"steps": _normalize_steps(payload.get("steps")),
	}


def extract_goal(
	context: ContextSnapshot,
	config: dict[str, Any],
	model_call: Callable[..., str],
) -> IntentResult:
	"""调用模型提取目标，并对不可信输出做结构化校验。

	Args:
		context: 上下文解析节点的输出。
		config: AI 配置。
		model_call: 文本模型调用函数。

	Returns:
		可供路由节点安全消费的意图结果。
	"""
	question = context["question"]
	prompt = f"""你是 BossHunter 的任务理解器。只返回一个 JSON 对象。
允许的 intent_type：{sorted(_INTENT_TYPES)}
字段必须包含：goal、mode、intent_type、entities、constraints、steps、missing_fields、confidence、risk_level、reason。
mode 只能是 answer、execute、draft、confirm、cancel，用于区分咨询、执行、起草、确认和取消。
steps 是按执行顺序排列的任务步骤，每项包含 id、action、arguments、depends_on、enabled、reason；
如果用户明确说“不要发送/只生成/先不要投递”，必须在 constraints 中保留对应限制，并将相关步骤 enabled 设为 false。
复杂请求要拆成多个步骤，不要只返回 complex_task；单一请求 steps 也要返回一个步骤。
confidence 范围为 0 到 1；risk_level 只能是 low、medium、high。
不确定或信息不足时，不要猜测实体，把字段名放入 missing_fields。
任何发送、投递行为必须标记 high；写库或启动后台任务至少标记 medium。
区分“咨询怎么做”和“要求执行”：面试策略、求职建议、流程解释属于 general_question；
只有用户明确要求代写给 HR 的具体消息时才是 reply_draft。reply_draft 可以直接使用用户描述，不要求 candidate_name 或 job_id。
当前岗位 ID：{context["current_job_id"] or "未选择"}
上一轮待补全意图：{json.dumps(context.get("pending_intent"), ensure_ascii=False)}
如果上一轮待补全意图存在，且当前消息是在回答缺失字段，必须延续原目标并补全实体，不要把它识别成新问题。
最近对话：{json.dumps(context["recent_messages"], ensure_ascii=False)}
用户问题：{question}"""
	try:
		raw_result = model_call(prompt, config, max_tokens=600)
	except Exception:
		fallback = apply_intent_policy(fallback_intent(question), question)
		return _fill_pending_intent(context, fallback)
	intent = apply_intent_policy(normalize_intent(_extract_json_object(raw_result), question), question)
	intent = _fill_pending_intent(context, intent)
	if context["current_job_id"] and intent["intent_type"] == "greeting_send" and not intent["entities"].get("job_ids"):
		intent["entities"] = {**intent["entities"], "job_ids": [context["current_job_id"]]}
	return intent


def is_complex_goal(intent: IntentResult) -> bool:
	"""判断目标是否需要 Planner 拆分为多个依赖步骤。

	Args:
		intent: 结构化意图。

	Returns:
		需要规划器时返回 True。
	"""
	if intent["intent_type"] in {"pipeline_run", "complex_task"}:
		return True
	if intent.get("steps"):
		return len([step for step in intent["steps"] if step["enabled"]]) >= 2
	goal = intent["goal"].replace(" ", "")
	action_count = sum(term in goal for term in ("采集", "搜索", "评分", "生成", "发送", "监测"))
	return action_count >= 2


__all__ = [
	"extract_goal",
	"fallback_intent",
	"is_complex_goal",
	"normalize_intent",
	"resolve_context",
]
