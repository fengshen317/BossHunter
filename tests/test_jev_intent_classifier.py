"""Jev 协议与影子评估回归测试，全程隔离真实网络。"""

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest

from bosshunter.ai.jev import JevError, evaluate, validate_response
from bosshunter.assistant import intent_classifier
from bosshunter.assistant.intent_classifier import build_classifier_state, build_questions, collect_shadow_evidence
from bosshunter.assistant.understanding import resolve_context


def _response() -> dict[str, Any]:
	"""构造完整合法答案，让测试关注协议与业务隔离。"""
	answers: dict[str, Any] = {}
	for name, question in build_questions().items():
		if question["type"] == "noul":
			answers[name] = {"type": "noul", "noul": 0.95}
		else:
			choice = next(iter(question["criteria"]))
			answers[name] = {
				"type": "choice",
				"choice": choice,
				"confidence": 0.8,
				"probabilities": {option: float(option == choice) for option in question["criteria"]},
			}
	return {"model": "jev-1.13.0", "answers": answers}


def _mock_http(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
	"""用真实 httpx 解析逻辑和内存传输验证请求。"""
	client_class = httpx.Client
	monkeypatch.setattr(
		"bosshunter.ai.jev.httpx.Client",
		lambda **kwargs: client_class(transport=httpx.MockTransport(handler), **kwargs),
	)


def test_client_uses_official_contract(monkeypatch: pytest.MonkeyPatch) -> None:
	"""中文输入使用正式 Choice/Noul 字段并仅调用一次。"""
	requests: list[httpx.Request] = []

	def handler(request: httpx.Request) -> httpx.Response:
		"""捕获请求而不接触公网。"""
		requests.append(request)
		assert request.url == "https://api.typesafe.ai/v1/systemone"
		assert request.headers["authorization"] == "Bearer test-secret"
		body = json.loads(request.content)
		assert body["model"] == "jev-latest"
		assert body["state"]["user_message"] == "帮我找深圳的 Python 岗位，先不要发送招呼语。"
		assert body["questions"]["do_not_send"]["type"] == "noul"
		return httpx.Response(200, json=_response())

	_mock_http(monkeypatch, handler)
	result = evaluate(
		{"user_message": "帮我找深圳的 Python 岗位，先不要发送招呼语。"},
		build_questions(),
		api_key="test-secret",
	)
	assert result["model"] == "jev-1.13.0"
	assert result["answers"]["intent"]["confidence"] == 0.8
	assert result["answers"]["intent"]["probabilities"]["job_search"] == 1.0
	assert len(requests) == 1


@pytest.mark.parametrize(
	"status,category",
	[
		(401, "authentication"),
		(403, "permission"),
		(422, "request_contract"),
		(429, "rate_limit"),
		(451, "region_unavailable"),
		(503, "server_error"),
		(302, "http_error"),
	],
)
def test_http_errors_are_safe_and_not_retried(monkeypatch: pytest.MonkeyPatch, status: int, category: str) -> None:
	"""鉴权、地区限制与重定向均不重试，不回显响应原文。"""
	requests: list[httpx.Request] = []

	def handler(request: httpx.Request) -> httpx.Response:
		"""模拟可能包含密钥的服务错误。"""
		requests.append(request)
		return httpx.Response(status, text="test-secret", headers={"location": "https://other.example"})

	_mock_http(monkeypatch, handler)
	with pytest.raises(JevError) as error:
		evaluate({}, build_questions(), api_key="test-secret")
	assert error.value.category == category
	assert error.value.status_code == status
	assert "test-secret" not in str(error.value)
	assert len(requests) == 1


@pytest.mark.parametrize("error_class,category", [(httpx.ReadTimeout, "timeout"), (httpx.ConnectError, "network")])
def test_network_errors_are_classified(monkeypatch: pytest.MonkeyPatch, error_class: Any, category: str) -> None:
	"""超时和连接失败使用稳定类别，不保存底层异常原文。"""

	def handler(request: httpx.Request) -> httpx.Response:
		"""模拟失败的网络边界。"""
		raise error_class("test-secret", request=request)

	_mock_http(monkeypatch, handler)
	with pytest.raises(JevError, match=category):
		evaluate({}, build_questions(), api_key="test-secret")


@pytest.mark.parametrize("bad_value", [True, float("nan"), float("inf"), -0.1, 1.1, "0.9", None])
def test_invalid_scores_are_rejected(bad_value: Any) -> None:
	"""非法分数不能进入影子证据。"""
	response = _response()
	response["answers"]["do_not_send"]["noul"] = bad_value
	with pytest.raises(JevError, match="invalid_response"):
		validate_response(response, build_questions())


@pytest.mark.parametrize("mutation", ["missing", "unknown_choice", "wrong_type", "distribution", "missing_model"])
def test_broken_contract_is_rejected(mutation: str) -> None:
	"""缺失答案、未知枚举和错误概率分布一律拒绝。"""
	response = _response()
	if mutation == "missing":
		del response["answers"]["mode"]
	elif mutation == "unknown_choice":
		response["answers"]["intent"]["choice"] = "unsafe_tool"
	elif mutation == "wrong_type":
		response["answers"]["intent"]["type"] = "boolean"
	elif mutation == "distribution":
		response["answers"]["intent"]["probabilities"]["job_search"] = 0.2
	else:
		del response["model"]
	with pytest.raises(JevError, match="invalid_response"):
		validate_response(response, build_questions())


def test_bad_json_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
	"""成功状态不能掩盖非法 JSON。"""
	_mock_http(monkeypatch, lambda request: httpx.Response(200, text="not-json"))
	with pytest.raises(JevError, match="invalid_response"):
		evaluate({}, build_questions(), api_key="test-secret")


def test_shadow_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
	"""默认路径不得产生额外模型请求。"""

	def unexpected_call(*args: Any, **kwargs: Any) -> dict[str, Any]:
		"""禁止默认配置调用 Jev。"""
		pytest.fail("默认模式不应调用 Jev")

	monkeypatch.setattr(intent_classifier, "evaluate", unexpected_call)
	assert collect_shadow_evidence(resolve_context({"messages": []}), {}) is None


def test_shadow_keeps_inputs_and_memory_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
	"""影子返回不同分类，也不能改写原上下文或上传长期记忆。"""
	context = resolve_context({"messages": [{"role": "user", "content": "只生成，不发送"}]})
	context["retrieved_memories"] = [{"content": "完整简历和秘密"}]
	before = deepcopy(context)
	monkeypatch.setenv("TYPESAFE_API_KEY", "test-secret")

	def fake_evaluate(state: dict[str, Any], questions: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
		"""确认最小输入不包含长期记忆。"""
		assert "retrieved_memories" not in state
		assert "完整简历和秘密" not in json.dumps(state, ensure_ascii=False)
		return _response()

	monkeypatch.setattr(intent_classifier, "evaluate", fake_evaluate)
	evidence = collect_shadow_evidence(context, {"assistant": {"intent_classifier": {"mode": "shadow"}}})
	assert evidence is not None and evidence["status"] == "completed"
	assert context == before
	assert "test-secret" not in json.dumps(evidence)
	assert "user_message" not in evidence


@pytest.mark.parametrize("category", ["timeout", "region_unavailable", "invalid_response", "missing_api_key"])
def test_shadow_failure_is_non_blocking(monkeypatch: pytest.MonkeyPatch, category: str) -> None:
	"""影子接口失败仍返回安全证据，不向上抛出异常。"""

	def failed_call(*args: Any, **kwargs: Any) -> dict[str, Any]:
		"""模拟客户端分类异常。"""
		raise JevError(category)

	monkeypatch.setattr(intent_classifier, "evaluate", failed_call)
	evidence = collect_shadow_evidence(
		resolve_context({"messages": []}),
		{"assistant": {"intent_classifier": {"mode": "shadow"}}},
	)
	assert evidence is not None and evidence["error_category"] == category


def test_cancellation_skips_network(monkeypatch: pytest.MonkeyPatch) -> None:
	"""取消后不启动影子请求。"""

	def unexpected_call(*args: Any, **kwargs: Any) -> dict[str, Any]:
		"""取消场景禁止发起请求。"""
		pytest.fail("取消后不应请求")

	monkeypatch.setattr(intent_classifier, "evaluate", unexpected_call)
	evidence = collect_shadow_evidence(
		resolve_context({"messages": []}),
		{"assistant": {"intent_classifier": {"mode": "shadow"}}},
		cancel_check=lambda: True,
	)
	assert evidence is not None and evidence["error_category"] == "cancelled"


def test_state_keeps_pending_contract_without_entity_values() -> None:
	"""待补全关系保留，但简历实体和任意参数不上传。"""
	context = resolve_context({"messages": [{"role": "user", "content": "换成深圳"}]})
	context["pending_intent"] = {
		"intent_type": "job_search",
		"mode": "execute",
		"missing_fields": ["cities"],
		"entities": {"resume": "私人简历"},
	}
	state = build_classifier_state(context)
	assert state["pending_intent"]["intent_type"] == "job_search"
	assert "私人简历" not in json.dumps(state, ensure_ascii=False)


@pytest.mark.parametrize("shadow_status", ["completed", "failed"])
def test_graph_shadow_never_changes_intent(
	monkeypatch: pytest.MonkeyPatch,
	tmp_path: Path,
	shadow_status: str,
) -> None:
	"""图节点在影子分歧或地区限制下仍保留原分类、约束和记忆操作。"""
	from bosshunter.assistant import graph
	from bosshunter.assistant.service import AssistantService
	from conftest import local_runtime_store

	config_path = tmp_path / "config.yaml"
	config_path.write_text("assistant:\n  intent_classifier:\n    mode: shadow\n", encoding="utf-8")
	context = resolve_context({"messages": [{"role": "user", "content": "只生成，不发送"}]})
	original = {
		"goal": "生成招呼语",
		"intent_type": "greeting_generate",
		"mode": "draft",
		"entities": {},
		"constraints": {"do_not_send": True},
		"steps": [],
		"missing_fields": [],
		"confidence": 0.9,
		"risk_level": "medium",
		"reason": "测试",
		"memory_operations": [],
	}
	before = deepcopy(original)
	evidence = {"status": shadow_status, "answers": _response()["answers"]}
	monkeypatch.setattr(graph, "extract_goal", lambda *args: original)
	monkeypatch.setattr(graph, "collect_shadow_evidence", lambda *args, **kwargs: evidence)
	with local_runtime_store(tmp_path):
		service = AssistantService(tmp_path, config_path)
		try:
			result = service.graph.builder.nodes["goal_extractor"].runnable.invoke({"context": context})
		finally:
			service.close()
	assert result["intent"] == before
	assert result["intent_classifier_evidence"]["status"] == shadow_status
	if shadow_status == "completed":
		assert result["intent_classifier_evidence"]["intent_agrees"] is False
