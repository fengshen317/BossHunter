"""Credential helpers for AI providers."""

import hashlib
import json
import os
import random
import re
import threading
import time
from collections.abc import Iterator, Callable
from typing import Any

import httpx


_MODEL_RESOLVE_CACHE: dict[tuple[str, str, str], str] = {}
# 保护 _MODEL_RESOLVE_CACHE 的并发读写，避免多线程同时探测 /v1/models 造成缓存 stampede。
_MODEL_RESOLVE_LOCK = threading.Lock()

# 单次 AI 请求默认超时（秒）。推理模型（deepseek-v4-flash 等）思维链较长，
# 旧的 60s 默认值会在思维链还没读完时就抛 "The read operation timed out"，
# 故默认放宽到 180s；用户可通过 ai.request_timeout 自行调整。
DEFAULT_REQUEST_TIMEOUT = 180
_MIN_REQUEST_TIMEOUT = 10
_MAX_REQUEST_TIMEOUT = 600
# Anthropic Messages API 强制要求 max_tokens，无法像 OpenAI-compatible 一样省略。
# 该值仅作为协议适配默认值，业务调用方不再各自设置输出上限。
DEFAULT_ANTHROPIC_MAX_TOKENS = 8192


def _request_timeout(ai_cfg: dict) -> int:
	"""读取并钳制 AI 请求超时（秒）。非法值回退默认值。"""
	try:
		raw = ai_cfg.get("request_timeout", DEFAULT_REQUEST_TIMEOUT)
		value = int(raw)
	except (TypeError, ValueError):
		return DEFAULT_REQUEST_TIMEOUT
	return max(_MIN_REQUEST_TIMEOUT, min(_MAX_REQUEST_TIMEOUT, value))


# 可重试的 HTTP 状态码：限流(429) + 服务端瞬时错误(5xx)。
# 404/401/403 等客户端错误不可重试（重试结果不变），保持原样抛出。
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
DEFAULT_HTTP_MAX_RETRIES = 3
DEFAULT_HTTP_BACKOFF_BASE = 2.0  # 指数退避基数（秒）：base * 2**attempt + jitter
DEFAULT_HTTP_BACKOFF_CAP = 30.0  # 单次退避上限（秒）
_MIN_HTTP_MAX_RETRIES = 0
_MAX_HTTP_MAX_RETRIES = 5


def _http_retry_settings(ai_cfg: dict) -> tuple[int, float, float]:
	"""读取 HTTP 重试设置：返回 (max_retries, backoff_base, backoff_cap)。

	max_retries 钳制到 [0, 5]；0 表示完全禁用重试（保持旧行为）。
	"""
	try:
		max_retries = int(ai_cfg.get("http_max_retries", DEFAULT_HTTP_MAX_RETRIES))
	except (TypeError, ValueError):
		max_retries = DEFAULT_HTTP_MAX_RETRIES
	return (
		max(_MIN_HTTP_MAX_RETRIES, min(_MAX_HTTP_MAX_RETRIES, max_retries)),
		DEFAULT_HTTP_BACKOFF_BASE,
		DEFAULT_HTTP_BACKOFF_CAP,
	)


def _sleep_backoff(attempt: int, backoff_base: float, backoff_cap: float) -> None:
	"""指数退避 + jitter：min(cap, base * 2**attempt) + uniform(0, base)。

	jitter 避免并发重试（招呼语默认并发 4）同步打到同一个限流窗口。
	"""
	delay = min(backoff_cap, backoff_base * (2 ** attempt)) + random.uniform(0, backoff_base)
	time.sleep(delay)


def _request_with_retry(
	request_fn: Callable[[], Any],
	ai_cfg: dict,
	retryable_statuses: frozenset[int] = _RETRYABLE_STATUS,
) -> Any:
	"""带 HTTP 层重试的请求包装：对 5xx/429 与网络瞬时错误指数退避，其余错误直接抛出。

	Args:
		request_fn: 无参请求函数；其 ``httpx.HTTPStatusError`` 会被按状态码分类。
		ai_cfg: AI 配置（读取 http_max_retries）。
		retryable_statuses: 可重试的状态码集合。

	Raises:
		httpx.HTTPStatusError: 非可重试状态码（如 404/401/403）立即抛出，
			保持原契约——调用方按语义处理，不被重试掩盖。
		httpx.TransportError / httpx.TimeoutException: 重试耗尽后抛出。
	"""
	max_retries, backoff_base, backoff_cap = _http_retry_settings(ai_cfg)
	last_exc: Exception | None = None
	for attempt in range(max_retries + 1):
		try:
			return request_fn()
		except httpx.HTTPStatusError as exc:
			status = exc.response.status_code
			if status not in retryable_statuses or attempt >= max_retries:
				raise
			last_exc = exc
		except (httpx.TransportError, httpx.TimeoutException) as exc:
			# 连接重置、DNS 抖动、读超时等网络层瞬时错误同样可重试。
			if attempt >= max_retries:
				raise
			last_exc = exc

		_sleep_backoff(attempt, backoff_base, backoff_cap)

	# 理论上不可达（循环内已 return/raise），保险起见把最后一次异常抛出。
	assert last_exc is not None
	raise last_exc


def get_anthropic_api_key(config: dict) -> str | None:
    """Resolve the Anthropic API key from env or config."""
    ai_cfg = config.get("ai", {}) if isinstance(config, dict) else {}
    return (
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or ai_cfg.get("api_key")
        or ai_cfg.get("auth_token")
    )


def build_anthropic_client_kwargs(config: dict) -> dict:
    """Build Anthropic SDK client kwargs from env and config."""
    ai_cfg = config.get("ai", {}) if isinstance(config, dict) else {}
    api_key = os.environ.get("ANTHROPIC_API_KEY") or ai_cfg.get("api_key")
    auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN") or ai_cfg.get("auth_token")

    kwargs = {}
    if api_key:
        kwargs["api_key"] = api_key
    if auth_token:
        kwargs["auth_token"] = auth_token

    base_url = os.environ.get("ANTHROPIC_BASE_URL") or ai_cfg.get("base_url")
    if base_url:
        kwargs["base_url"] = base_url

    # 仅当显式配置 request_timeout 时才注入，避免覆盖 SDK 默认超时；
    # 旧版本 anthropic 路径未设置 timeout，依赖 SDK 默认（约 10 分钟）。
    if "request_timeout" in ai_cfg:
        kwargs["timeout"] = _request_timeout(ai_cfg)

    return kwargs


def resolve_anthropic_model(model: str, config: dict) -> str:
    """Resolve configured model name against compatible API model IDs when needed."""
    ai_cfg = config.get("ai", {}) if isinstance(config, dict) else {}
    base_url = os.environ.get("ANTHROPIC_BASE_URL") or ai_cfg.get("base_url")
    if not base_url:
        return model

    auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN") or ai_cfg.get("auth_token")
    api_key = os.environ.get("ANTHROPIC_API_KEY") or ai_cfg.get("api_key")
    cache_key = (base_url.rstrip("/"), model, _credential_fingerprint(auth_token or api_key or ""))

    # 双重检查：先无锁读缓存（快路径），未命中再进锁内二次检查 + 探测，
    # 避免并发下多个线程同时打 /v1/models。
    cached = _MODEL_RESOLVE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    with _MODEL_RESOLVE_LOCK:
        # 进锁后再查一次，防止已在锁外排队时其他线程已填充。
        cached = _MODEL_RESOLVE_CACHE.get(cache_key)
        if cached is not None:
            return cached

        headers = {}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        elif api_key:
            headers["x-api-key"] = api_key

        try:
            response = httpx.get(f"{base_url.rstrip('/')}/v1/models", headers=headers, timeout=10)
            response.raise_for_status()
            model_ids = [item.get("id", "") for item in response.json().get("data", [])]
            resolved = _match_model_name(model, model_ids) or model
            # 只缓存"探测成功"的结果。失败（超时/DNS/5xx）不缓存，否则一次
            # 瞬时抖动会让整个进程生命周期内都拿不到正确模型名（负缓存）。
            _MODEL_RESOLVE_CACHE[cache_key] = resolved
            return resolved
        except Exception:
            return model


def _anthropic_client(config: dict):
	"""构建 Anthropic SDK 客户端，强制注入超时与重试上限。

	SDK 默认超时约 10 分钟且不自动重试，一次挂起的调用会卡住调用线程很久；
	这里始终注入 180s 请求超时（可用 ai.request_timeout 调整）与 max_retries
	（默认 3，走 SDK 自身的指数退避）。
	"""
	import anthropic

	ai_cfg = config.get("ai", {}) if isinstance(config, dict) else {}
	kwargs = build_anthropic_client_kwargs(config)
	kwargs["timeout"] = _request_timeout(ai_cfg)
	max_retries, _, _ = _http_retry_settings(ai_cfg)
	kwargs["max_retries"] = max_retries
	return anthropic.Anthropic(**kwargs)


def call_anthropic_text(prompt: str, config: dict, max_tokens: int | None = None) -> str | None:
	"""Call Anthropic-compatible Messages API and return the first text block."""
	ai_cfg = config.get("ai", {}) if isinstance(config, dict) else {}
	if ai_cfg.get("provider") == "openai_compatible":
		return call_openai_compatible_text(prompt, config)

	try:
		import anthropic
	except ImportError:
		return None

	if not get_anthropic_api_key(config):
		return None

	model = resolve_anthropic_model(ai_cfg.get("model", "claude-sonnet-4-6"), config)
	client = _anthropic_client(config)
	required_max_tokens = int(ai_cfg.get("anthropic_max_tokens") or max_tokens or DEFAULT_ANTHROPIC_MAX_TOKENS)
	response = client.messages.create(
		model=model,
		max_tokens=required_max_tokens,
		messages=[{"role": "user", "content": prompt}],
	)
	for block in response.content:
		text = getattr(block, "text", None)
		if text is not None:
			return text.strip()
	return None


def call_ai_with_tools(
	prompt: str,
	config: dict[str, Any],
	tools: list[dict[str, Any]],
	max_tokens: int | None = None,
) -> dict[str, Any]:
	"""调用模型并归一化返回文本与原生工具调用。

	OpenAI 兼容接口使用 ``tools`` / ``tool_calls`` 标准字段。Anthropic
	路径保留文本回答兼容性；其调用方可安全降级为不执行工具。
	"""
	ai_cfg = config.get("ai", {}) if isinstance(config, dict) else {}
	if ai_cfg.get("provider") != "openai_compatible":
		return {"content": call_anthropic_text(prompt, config, max_tokens) or "", "tool_calls": []}

	api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or ai_cfg.get("api_key")
	base_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL") or ai_cfg.get("base_url")
	if not api_key or not base_url:
		return {"content": "", "tool_calls": []}

	def _request() -> dict[str, Any]:
		response = httpx.post(
			f"{base_url.rstrip('/')}/chat/completions",
			headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
			json={
				"model": ai_cfg.get("model", "deepseek-chat"),
				"messages": [{"role": "user", "content": prompt}],
				"tools": tools,
				"tool_choice": "auto",
				"temperature": 0.1,
			},
			timeout=_request_timeout(ai_cfg),
		)
		response.raise_for_status()
		choices = response.json().get("choices", [])
		if not choices:
			return {"content": "", "tool_calls": []}
		message = choices[0].get("message", {})
		calls: list[dict[str, Any]] = []
		for call in message.get("tool_calls", []) or []:
			function = call.get("function", {})
			try:
				arguments = json.loads(function.get("arguments", "{}"))
			except (TypeError, ValueError):
				continue
			if isinstance(arguments, dict) and isinstance(function.get("name"), str):
				calls.append({"id": str(call.get("id", "")), "name": function["name"], "arguments": arguments})
		return {"content": str(message.get("content") or ""), "tool_calls": calls}

	return _request_with_retry(_request, ai_cfg)


def stream_ai_text(prompt: str, config: dict, max_tokens: int | None = None) -> Iterator[str]:
	"""调用已配置模型并逐块返回正文。

	Args:
		prompt: 完整提示词。
		config: BossHunter 配置。
		max_tokens: 仅为兼容旧调用保留；业务层通常不应传入。

	Yields:
		模型实时返回的文本增量。
	"""
	ai_cfg = config.get("ai", {}) if isinstance(config, dict) else {}
	if ai_cfg.get("provider") == "openai_compatible":
		yield from _stream_openai_compatible_text(prompt, config)
		return

	try:
		import anthropic
	except ImportError:
		return
	if not get_anthropic_api_key(config):
		return

	model = resolve_anthropic_model(ai_cfg.get("model", "claude-sonnet-4-6"), config)
	client = _anthropic_client(config)
	required_max_tokens = int(ai_cfg.get("anthropic_max_tokens") or max_tokens or DEFAULT_ANTHROPIC_MAX_TOKENS)
	with client.messages.stream(
		model=model,
		max_tokens=required_max_tokens,
		messages=[{"role": "user", "content": prompt}],
	) as stream:
		yield from stream.text_stream


def _stream_openai_compatible_text(prompt: str, config: dict) -> Iterator[str]:
	"""解析 OpenAI 兼容接口的 SSE 文本增量。

	在生成器内部对 5xx/429 与网络瞬时错误做指数退避重试（流在出错时已结束，
	只能整体重发请求）。若模型返回空正文，则同样按可重试的空结果重试一次
	（推理模型偶发空完成），全部尝试无正文时 yield 结束。
	"""
	ai_cfg = config.get("ai", {}) if isinstance(config, dict) else {}
	api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or ai_cfg.get("api_key")
	base_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL") or ai_cfg.get("base_url")
	if not api_key or not base_url:
		return

	url = f"{base_url.rstrip('/')}/chat/completions"
	headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
	model = ai_cfg.get("model", "deepseek-chat")
	timeout = _request_timeout(ai_cfg)
	max_retries, backoff_base, backoff_cap = _http_retry_settings(ai_cfg)
	# 空正文也值得重试一次：推理模型偶发"只吐 reasoning 无 content"的完成。
	empty_retries = 1

	last_exc: Exception | None = None
	for attempt in range(max_retries + 1):
		print(f"[AI助手] 模型流请求开始：model={model}，attempt={attempt + 1}", flush=True)
		emitted_chars = 0
		reasoning_chars = 0
		finish_reason = None
		try:
			with httpx.stream(
				"POST",
				url,
				headers=headers,
				json={
					"model": model,
					"messages": [{"role": "user", "content": prompt}],
					"temperature": 0.2,
					"stream": True,
				},
				timeout=timeout,
			) as response:
				response.raise_for_status()
				for line in response.iter_lines():
					if not line.startswith("data: "):
						continue
					payload = line[6:].strip()
					if payload == "[DONE]":
						break
					try:
						event = json.loads(payload)
					except (ValueError, TypeError):
						continue
					choices = event.get("choices", [])
					if not choices:
						continue
					choice = choices[0]
					finish_reason = choice.get("finish_reason") or finish_reason
					delta_payload = choice.get("delta", {})
					reasoning = delta_payload.get("reasoning_content")
					if isinstance(reasoning, str):
						reasoning_chars += len(reasoning)
					delta = delta_payload.get("content")
					if isinstance(delta, str) and delta:
						emitted_chars += len(delta)
						# 必须在读取下一条网络事件前交给 SSE 层，避免“伪流式”。
						yield delta
		except httpx.HTTPStatusError as exc:
			status = exc.response.status_code
			if emitted_chars or status not in _RETRYABLE_STATUS or attempt >= max_retries:
				raise
			last_exc = exc
		except (httpx.TransportError, httpx.TimeoutException) as exc:
			# 已经向用户显示正文后不能整体重放，否则会产生重复段落。
			if emitted_chars or attempt >= max_retries:
				raise
			last_exc = exc
		else:
			print(
				f"[AI助手] 模型流请求结束：正文={emitted_chars} 字，推理={reasoning_chars} 字，"
				f"finish_reason={finish_reason or 'unknown'}",
				flush=True,
			)
			if emitted_chars:
				return
			# 空正文：仅在剩余空正文重试额度内重试。
			if attempt >= empty_retries:
				print("[AI助手] 流请求多次无正文，结束本次流。", flush=True)
				return
			last_exc = None

		_sleep_backoff(attempt, backoff_base, backoff_cap)

	# 循环内已 raise/return，理论上不可达。
	assert last_exc is not None
	raise last_exc


def call_openai_compatible_text(prompt: str, config: dict, max_tokens: int | None = None) -> str | None:
	"""调用 OpenAI-compatible Chat Completions，且不设置输出 token 上限。

	``max_tokens`` 仅为兼容旧调用保留，不会写入请求体。
	"""
	ai_cfg = config.get("ai", {}) if isinstance(config, dict) else {}
	api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or ai_cfg.get("api_key")
	base_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL") or ai_cfg.get("base_url")
	model = ai_cfg.get("model", "deepseek-chat")
	if not api_key or not base_url:
		return None

	url = f"{base_url.rstrip('/')}/chat/completions"
	headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
	# 推理模型思维链较长，旧的硬编码 60s 会触发 "The read operation timed out"；
	# 默认 180s，用户可通过 ai.request_timeout 调整。
	timeout = _request_timeout(ai_cfg)

	def _request() -> tuple[str | None, str | None]:
		"""发一次请求，返回 (content, finish_reason)；content 为 None 表示非字符串或无 choices。"""
		response = httpx.post(
			url,
			headers=headers,
			json={
				"model": model,
				"messages": [{"role": "user", "content": prompt}],
				"temperature": 0.2,
			},
			timeout=timeout,
		)
		response.raise_for_status()
		choices = response.json().get("choices", [])
		if not choices:
			return None, None
		first = choices[0]
		content = first.get("message", {}).get("content")
		text = content.strip() if isinstance(content, str) else None
		return text, first.get("finish_reason")

	content, _ = _request_with_retry(_request, ai_cfg)
	return content


def _match_model_name(requested: str, available: list[str]) -> str | None:
    if requested in available:
        return requested

    requested_norm = _normalize_model_name(requested)
    for candidate in available:
        if _normalize_model_name(candidate) == requested_norm:
            return candidate

    for candidate in available:
        candidate_norm = _normalize_model_name(candidate)
        if requested_norm in candidate_norm or candidate_norm in requested_norm:
            return candidate

    requested_tokens = _model_tokens(requested)
    for candidate in available:
        if requested_tokens and requested_tokens <= _model_tokens(candidate):
            return candidate

    return None


def _credential_fingerprint(credential: str) -> str:
    return hashlib.sha256(credential.encode("utf-8")).hexdigest() if credential else ""


def _normalize_model_name(name: str) -> str:
    return re.sub(r"[\s._-]+", "", name).lower()


def _model_tokens(name: str) -> set[str]:
    return {token for token in re.split(r"[\s._-]+", name.lower()) if token}
