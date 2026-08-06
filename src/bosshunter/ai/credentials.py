"""Credential helpers for AI providers."""

import hashlib
import os
import random
import re
import threading
import time

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
        except Exception:
            _MODEL_RESOLVE_CACHE[cache_key] = model
            return model

        resolved = _match_model_name(model, model_ids) or model
        _MODEL_RESOLVE_CACHE[cache_key] = resolved
        return resolved


def call_anthropic_text(prompt: str, config: dict, max_tokens: int) -> str | None:
    """Call Anthropic-compatible Messages API and return the first text block."""
    ai_cfg = config.get("ai", {}) if isinstance(config, dict) else {}
    if ai_cfg.get("provider") == "openai_compatible":
        return call_openai_compatible_text(prompt, config, max_tokens)

    try:
        import anthropic
    except ImportError:
        return None

    if not get_anthropic_api_key(config):
        return None

    model = resolve_anthropic_model(ai_cfg.get("model", "claude-sonnet-4-6"), config)
    client = anthropic.Anthropic(**build_anthropic_client_kwargs(config))
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    for block in response.content:
        text = getattr(block, "text", None)
        if text is not None:
            return text.strip()
    return None


def call_openai_compatible_text(prompt: str, config: dict, max_tokens: int) -> str | None:
    """Call an OpenAI-compatible chat completions endpoint.

    对推理类模型（如 deepseek-v4-flash，会在 reasoning_content 字段输出思维链）做兜底：
    当响应成功但正式 content 为空时，通常是思维链挤占了 max_tokens 预算，故自动以更大
    的 max_tokens 重试一次，避免上层把空字符串误判为"生成失败"。
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
    # 5xx/429 与网络层瞬时错误（连接重置、读超时）自动指数退避重试。
    max_retries, backoff_base, backoff_cap = _http_retry_settings(ai_cfg)

    def _request(budget: int) -> tuple[str | None, str | None]:
        """发一次请求，返回 (content, finish_reason)；content 为 None 表示非字符串或无 choices。"""
        response = httpx.post(
            url,
            headers=headers,
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": budget,
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

    def _request_with_retry(budget: int) -> tuple[str | None, str | None]:
        """带 HTTP 层重试的请求：对 5xx/429 与网络瞬时错误指数退避，其余错误直接抛出。

        非可重试状态码（如 404/401/403）保持原契约——立即向上抛 HTTPStatusError，
        让调用方按语义处理，不被重试掩盖。
        """
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                return _request(budget)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status not in _RETRYABLE_STATUS or attempt >= max_retries:
                    raise
                last_exc = exc
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                # 连接重置、DNS 抖动、读超时等网络层瞬时错误同样可重试。
                if attempt >= max_retries:
                    raise
                last_exc = exc

            # 指数退避 + jitter：min(cap, base * 2**attempt) + uniform(0, base)。
            # jitter 避免并发重试（招呼语默认并发 4）同步打到同一个限流窗口。
            delay = min(backoff_cap, backoff_base * (2 ** attempt)) + random.uniform(0, backoff_base)
            time.sleep(delay)

        # 理论上不可达（循环内已 return/raise），保险起见把最后一次异常抛出。
        assert last_exc is not None
        raise last_exc

    content, finish_reason = _request_with_retry(max_tokens)

    # content 为空但响应成功：常见于推理模型思维链挤占预算（finish_reason 为 "length"），
    # 也可能是兼容接口偶发返回空 content。给予 4 倍 max_tokens 重试一次，仍为空才放弃。
    if not content:
        budget = max_tokens * 4 if finish_reason == "length" else max_tokens * 2
        content, _ = _request_with_retry(budget)

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
