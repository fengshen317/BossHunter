"""Jev 专用协议客户端，不复用聊天模型的文本生成接口。"""

import math
from typing import Any

import httpx


DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_TIMEOUT_SECONDS = 3.0
MAX_RESPONSE_BYTES = 256_000


class JevError(Exception):
	"""仅携带安全错误类别，避免异常输出泄露鉴权头。"""

	def __init__(self, category: str, status_code: int | None = None) -> None:
		"""创建不含服务原文或密钥的调用错误。"""
		self.category = category
		self.status_code = status_code
		super().__init__(category)


def _probability(value: Any) -> float:
	"""校验服务端数值，防止 NaN 或布尔值进入决策记录。"""
	if isinstance(value, bool) or not isinstance(value, (int, float)):
		raise JevError("invalid_response")
	result = float(value)
	if not math.isfinite(result) or not 0 <= result <= 1:
		raise JevError("invalid_response")
	return result


def validate_response(payload: Any, questions: dict[str, Any]) -> dict[str, Any]:
	"""校验所有预期答案，保留概率和置信度的独立含义。

	Args:
		payload: 服务端返回的数据。
		questions: 本次请求的问题契约。

	Returns:
		仅包含已校验答案和实际模型名的数据。
	"""
	if not isinstance(payload, dict) or not isinstance(payload.get("model"), str) or not payload["model"]:
		raise JevError("invalid_response")
	answers = payload.get("answers")
	if not isinstance(answers, dict):
		raise JevError("invalid_response")
	validated: dict[str, Any] = {}
	for name, question in questions.items():
		answer = answers.get(name)
		if not isinstance(answer, dict) or answer.get("type") != question["type"]:
			raise JevError("invalid_response")
		if question["type"] == "noul":
			validated[name] = {"type": "noul", "noul": _probability(answer.get("noul"))}
		elif question["type"] == "choice":
			criteria = question["criteria"]
			choice = answer.get("choice")
			probabilities = answer.get("probabilities")
			if not isinstance(choice, str) or choice not in criteria:
				raise JevError("invalid_response")
			if not isinstance(probabilities, dict) or set(probabilities) != set(criteria):
				raise JevError("invalid_response")
			values = {key: _probability(value) for key, value in probabilities.items()}
			if not math.isclose(sum(values.values()), 1.0, abs_tol=0.01):
				raise JevError("invalid_response")
			validated[name] = {
				"type": "choice",
				"choice": choice,
				"confidence": _probability(answer.get("confidence")),
				"probabilities": values,
			}
		else:
			raise JevError("invalid_question")
	return {"model": payload["model"], "answers": validated}


def evaluate(
	state: dict[str, Any],
	questions: dict[str, Any],
	*,
	api_key: str,
	model: str = "jev-latest",
	base_url: str = DEFAULT_BASE_URL,
	timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
	"""调用 Jev 一次，失败不重试，也不返回未经校验的结果。

	Args:
		state: 已裁剪的待分类上下文。
		questions: Choice 和 Noul 问题。
		api_key: 仅存在于进程内存的密钥。
		model: 模型名称。
		base_url: 服务根地址。
		timeout_seconds: 各网络阶段的超时上限。

	Returns:
		经过契约校验的答案。
	"""
	if not api_key:
		raise JevError("missing_api_key")
	if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
		raise JevError("invalid_config")
	url = httpx.URL(base_url)
	if url.scheme != "https" or not url.host or url.userinfo or url.query or url.fragment:
		raise JevError("invalid_config")
	try:
		# 不跟随重定向，避免鉴权信息被转发到配置以外的服务。
		with httpx.Client(timeout=timeout_seconds, follow_redirects=False) as client:
			response = client.post(
				f"{str(url).rstrip('/')}/v1/systemone",
				headers={"Authorization": f"Bearer {api_key}"},
				json={"state": state, "model": model, "questions": questions},
			)
		if response.status_code != 200:
			category = {
				401: "authentication",
				403: "permission",
				422: "request_contract",
				429: "rate_limit",
				451: "region_unavailable",
			}.get(response.status_code, "server_error" if response.status_code >= 500 else "http_error")
			raise JevError(category, response.status_code)
		if len(response.content) > MAX_RESPONSE_BYTES:
			raise JevError("invalid_response")
		return validate_response(response.json(), questions)
	except httpx.TimeoutException:
		raise JevError("timeout") from None
	except httpx.HTTPError:
		raise JevError("network") from None
	except ValueError:
		raise JevError("invalid_response") from None
