import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from bosshunter.ai import credentials


class AnthropicCredentialTests(unittest.TestCase):
    def setUp(self):
        cache = getattr(credentials, "_MODEL_RESOLVE_CACHE", None)
        if cache is not None:
            cache.clear()

    def test_prefers_documented_api_key_env_var(self):
        with patch.dict(
            "os.environ",
            {
                "ANTHROPIC_API_KEY": "from-api-key",
                "ANTHROPIC_AUTH_TOKEN": "from-auth-token",
            },
            clear=True,
        ):
            result = credentials.get_anthropic_api_key({"ai": {"api_key": "from-config"}})

        self.assertEqual(result, "from-api-key")

    def test_keeps_auth_token_as_backward_compatible_fallback(self):
        with patch.dict("os.environ", {"ANTHROPIC_AUTH_TOKEN": "from-auth-token"}, clear=True):
            result = credentials.get_anthropic_api_key({"ai": {"api_key": "from-config"}})

        self.assertEqual(result, "from-auth-token")

    def test_falls_back_to_config_api_key(self):
        with patch.dict("os.environ", {}, clear=True):
            result = credentials.get_anthropic_api_key({"ai": {"api_key": "from-config"}})

        self.assertEqual(result, "from-config")

    def test_falls_back_to_config_auth_token(self):
        with patch.dict("os.environ", {}, clear=True):
            result = credentials.get_anthropic_api_key({"ai": {"auth_token": "from-config-token"}})

        self.assertEqual(result, "from-config-token")

    def test_build_anthropic_client_kwargs_includes_auth_token_when_configured(self):
        with patch.dict(
            "os.environ",
            {
                "ANTHROPIC_API_KEY": "from-api-key",
                "ANTHROPIC_AUTH_TOKEN": "from-auth-token",
                "ANTHROPIC_BASE_URL": "https://api-gateway.example.com",
            },
            clear=True,
        ):
            build_kwargs = getattr(
                credentials,
                "build_anthropic_client_kwargs",
                lambda config: {"api_key": credentials.get_anthropic_api_key(config)},
            )
            result = build_kwargs({"ai": {}})

        self.assertEqual(result["api_key"], "from-api-key")
        self.assertEqual(result["auth_token"], "from-auth-token")
        self.assertEqual(result["base_url"], "https://api-gateway.example.com")

    def test_build_anthropic_client_kwargs_does_not_duplicate_auth_token_as_api_key(self):
        with patch.dict("os.environ", {}, clear=True):
            result = credentials.build_anthropic_client_kwargs({"ai": {"auth_token": "from-config-token"}})

        self.assertNotIn("api_key", result)
        self.assertEqual(result["auth_token"], "from-config-token")

    def test_compatible_api_model_cache_key_does_not_store_raw_credentials(self):
        with (
            patch.dict(
                "os.environ",
                {
                    "ANTHROPIC_BASE_URL": "https://api-gateway.example.com",
                    "ANTHROPIC_AUTH_TOKEN": "very-secret-token",
                },
                clear=True,
            ),
            patch("httpx.get", side_effect=RuntimeError("network down")),
        ):
            credentials.resolve_anthropic_model("claude-sonnet-4-6", {"ai": {}})

        cache_keys = list(credentials._MODEL_RESOLVE_CACHE)
        self.assertEqual(len(cache_keys), 1)
        self.assertNotIn("very-secret-token", cache_keys[0])

    def test_resolves_compatible_api_model_name_from_available_models(self):
        class ModelsResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"data": [{"id": "Claude Sonnet 4.6"}]}

        with (
            patch.dict(
                "os.environ",
                {
                    "ANTHROPIC_BASE_URL": "https://api-gateway.example.com",
                    "ANTHROPIC_API_KEY": "from-api-key",
                    "ANTHROPIC_AUTH_TOKEN": "from-auth-token",
                },
                clear=True,
            ),
            patch("httpx.get", return_value=ModelsResponse()),
        ):
            result = getattr(credentials, "resolve_anthropic_model", lambda model, config: model)("claude-sonnet-4-6", {"ai": {}})

        self.assertEqual(result, "Claude Sonnet 4.6")

    def test_caches_compatible_api_model_resolution_failures(self):
        with (
            patch.dict(
                "os.environ",
                {
                    "ANTHROPIC_BASE_URL": "https://api-gateway.example.com",
                    "ANTHROPIC_AUTH_TOKEN": "token-a",
                },
                clear=True,
            ),
            patch("httpx.get", side_effect=RuntimeError("network down")) as models_get,
        ):
            first = getattr(credentials, "resolve_anthropic_model", lambda model, config: model)("claude-sonnet-4-6", {"ai": {}})
            second = getattr(credentials, "resolve_anthropic_model", lambda model, config: model)("claude-sonnet-4-6", {"ai": {}})

        self.assertEqual(first, "claude-sonnet-4-6")
        self.assertEqual(second, "claude-sonnet-4-6")
        self.assertEqual(models_get.call_count, 1)

    def test_compatible_api_model_cache_is_separated_by_auth_token(self):
        class ModelsResponse:
            def __init__(self, model):
                self.model = model

            def raise_for_status(self):
                pass

            def json(self):
                return {"data": [{"id": self.model}]}

        with patch.dict(
            "os.environ",
            {
                "ANTHROPIC_BASE_URL": "https://api-gateway.example.com",
                "ANTHROPIC_AUTH_TOKEN": "token-a",
            },
            clear=True,
        ):
            with patch("httpx.get", return_value=ModelsResponse("Claude Sonnet 4.6")):
                first = getattr(credentials, "resolve_anthropic_model", lambda model, config: model)("claude-sonnet-4-6", {"ai": {}})

        with patch.dict(
            "os.environ",
            {
                "ANTHROPIC_BASE_URL": "https://api-gateway.example.com",
                "ANTHROPIC_AUTH_TOKEN": "token-b",
            },
            clear=True,
        ):
            with patch("httpx.get", return_value=ModelsResponse("Claude Sonnet 4.6 B")):
                second = getattr(credentials, "resolve_anthropic_model", lambda model, config: model)("claude-sonnet-4-6", {"ai": {}})

        self.assertEqual(first, "Claude Sonnet 4.6")
        self.assertEqual(second, "Claude Sonnet 4.6 B")

    def test_call_anthropic_text_uses_resolved_model_and_auth_token(self):
        calls = {}

        class Client:
            def __init__(self, **kwargs):
                calls["kwargs"] = kwargs
                self.messages = self

            def create(self, **kwargs):
                calls["message"] = kwargs
                return SimpleNamespace(content=[SimpleNamespace(text=" ok ")])

        call_text = getattr(credentials, "call_anthropic_text", lambda prompt, config, max_tokens: None)

        with (
            patch.dict("os.environ", {}, clear=True),
            patch.object(credentials, "resolve_anthropic_model", lambda model, config: "Claude Sonnet 4.6", create=True),
            patch.object(credentials, "build_anthropic_client_kwargs", lambda config: {"api_key": "key", "auth_token": "token", "base_url": "https://api-gateway.example.com"}, create=True),
            patch.dict("sys.modules", {"anthropic": SimpleNamespace(Anthropic=Client)}),
        ):
            result = call_text("prompt", {"ai": {"model": "claude-sonnet-4-6", "api_key": "key", "auth_token": "token"}}, 123)

        self.assertEqual(result, "ok")
        self.assertEqual(calls["kwargs"]["auth_token"], "token")
        self.assertEqual(calls["message"]["model"], "Claude Sonnet 4.6")
        self.assertEqual(calls["message"]["max_tokens"], 123)

    def test_openai_compatible_404_is_not_silently_swallowed(self):
        request = httpx.Request("POST", "https://api.example.com/chat/completions")
        response = httpx.Response(404, request=request)

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("httpx.post", return_value=response),
            self.assertRaises(httpx.HTTPStatusError),
        ):
            credentials.call_openai_compatible_text(
                "prompt",
                {
                    "ai": {
                        "provider": "openai_compatible",
                        "api_key": "secret",
                        "base_url": "https://api.example.com",
                        "model": "model",
                    }
                },
                256,
            )

    def test_openai_compatible_uses_default_timeout_when_unconfigured(self):
        class OkResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("httpx.post", return_value=OkResponse()) as posted,
        ):
            credentials.call_openai_compatible_text(
                "prompt",
                {"ai": {"api_key": "secret", "base_url": "https://api.example.com", "model": "model"}},
                256,
            )

        # 未配置 request_timeout 时应回退到 DEFAULT_REQUEST_TIMEOUT（旧的 60s 太短）。
        self.assertEqual(posted.call_args.kwargs["timeout"], credentials.DEFAULT_REQUEST_TIMEOUT)

    def test_openai_compatible_passes_configured_timeout_to_httpx(self):
        class OkResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("httpx.post", return_value=OkResponse()) as posted,
        ):
            credentials.call_openai_compatible_text(
                "prompt",
                {"ai": {"api_key": "secret", "base_url": "https://api.example.com", "model": "model", "request_timeout": 240}},
                256,
            )

        self.assertEqual(posted.call_args.kwargs["timeout"], 240)

    def test_openai_compatible_clamps_invalid_timeout_to_default(self):
        class OkResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("httpx.post", return_value=OkResponse()) as posted,
        ):
            credentials.call_openai_compatible_text(
                "prompt",
                {"ai": {"api_key": "secret", "base_url": "https://api.example.com", "model": "model", "request_timeout": "not-a-number"}},
                256,
            )

        self.assertEqual(posted.call_args.kwargs["timeout"], credentials.DEFAULT_REQUEST_TIMEOUT)

    def test_build_anthropic_client_kwargs_includes_timeout_when_configured(self):
        with patch.dict("os.environ", {}, clear=True):
            result = credentials.build_anthropic_client_kwargs({"ai": {"request_timeout": 200}})

        self.assertEqual(result["timeout"], 200)

    def test_build_anthropic_client_kwargs_omits_timeout_when_unconfigured(self):
        with patch.dict("os.environ", {}, clear=True):
            result = credentials.build_anthropic_client_kwargs({"ai": {}})

        self.assertNotIn("timeout", result)

    def test_openai_compatible_retries_on_503_then_succeeds(self):
        request = httpx.Request("POST", "https://api.example.com/chat/completions")
        resp_503 = httpx.Response(503, request=request)
        resp_200 = httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]},
        )

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("httpx.post", side_effect=[resp_503, resp_200]) as posted,
            patch("time.sleep"),  # 避免测试真的睡眠
        ):
            result = credentials.call_openai_compatible_text(
                "prompt",
                {"ai": {"api_key": "secret", "base_url": "https://api.example.com", "model": "model"}},
                256,
            )

        self.assertEqual(result, "hi")
        self.assertEqual(posted.call_count, 2)

    def test_openai_compatible_retries_on_429_with_backoff(self):
        request = httpx.Request("POST", "https://api.example.com/chat/completions")
        resp_429 = httpx.Response(429, request=request)
        resp_200 = httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        )

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("httpx.post", side_effect=[resp_429, resp_429, resp_200]) as posted,
            patch("time.sleep") as slept,
        ):
            result = credentials.call_openai_compatible_text(
                "prompt",
                {"ai": {"api_key": "secret", "base_url": "https://api.example.com", "model": "model"}},
                256,
            )

        self.assertEqual(result, "ok")
        self.assertEqual(posted.call_count, 3)
        # 重试 2 次 → 退避 2 次。
        self.assertEqual(slept.call_count, 2)

    def test_openai_compatible_exhausts_retries_then_raises_on_persistent_503(self):
        request = httpx.Request("POST", "https://api.example.com/chat/completions")
        resp_503 = httpx.Response(503, request=request)

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("httpx.post", return_value=resp_503) as posted,
            patch("time.sleep"),
            self.assertRaises(httpx.HTTPStatusError),
        ):
            credentials.call_openai_compatible_text(
                "prompt",
                {"ai": {"api_key": "secret", "base_url": "https://api.example.com", "model": "model"}},
                256,
            )

        # 默认 max_retries=3 → 总调用 = 首次 + 3 次重试 = 4 次。
        self.assertEqual(posted.call_count, credentials.DEFAULT_HTTP_MAX_RETRIES + 1)

    def test_openai_compatible_does_not_retry_on_404(self):
        # 守护契约：非可重试状态码（404）必须立即抛出，不重试。
        request = httpx.Request("POST", "https://api.example.com/chat/completions")
        resp_404 = httpx.Response(404, request=request)

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("httpx.post", return_value=resp_404) as posted,
            patch("time.sleep") as slept,
            self.assertRaises(httpx.HTTPStatusError),
        ):
            credentials.call_openai_compatible_text(
                "prompt",
                {"ai": {"api_key": "secret", "base_url": "https://api.example.com", "model": "model"}},
                256,
            )

        self.assertEqual(posted.call_count, 1)
        slept.assert_not_called()


if __name__ == "__main__":
    unittest.main()
