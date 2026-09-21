"""Browser Runtime HTTP 客户端鉴权测试。"""

from unittest.mock import Mock, patch

from bosshunter.browser.client import RuntimeClient


@patch("bosshunter.browser.client.httpx.get")
def test_runtime_client_adds_bearer_token_to_get_requests(http_get: Mock) -> None:
	"""配置 Token 后所有 GET 请求必须携带 Bearer Header。"""
	response = Mock(status_code=200)
	response.json.return_value = {"status": "ok"}
	http_get.return_value = response
	client = RuntimeClient(
		{"browser": {"proxy_host": "host.docker.internal", "proxy_port": 3456, "proxy_token": "secret"}}
	)

	client.health()

	assert http_get.call_args.kwargs["headers"] == {"Authorization": "Bearer secret"}


@patch("bosshunter.browser.client.httpx.post")
def test_runtime_client_adds_bearer_token_to_post_requests(http_post: Mock) -> None:
	"""写操作不能遗漏 Browser Runtime鉴权。"""
	http_post.return_value = Mock(status_code=200)
	client = RuntimeClient({"browser": {"proxy_token": "secret"}})

	assert client.click("target", "button") is True

	assert http_post.call_args.kwargs["headers"] == {"Authorization": "Bearer secret"}


@patch("bosshunter.browser.client.httpx.get")
def test_runtime_client_omits_authorization_without_token(http_get: Mock) -> None:
	"""本地回环兼容模式不发送空 Authorization Header。"""
	response = Mock(status_code=200)
	response.json.return_value = {"status": "ok"}
	http_get.return_value = response
	client = RuntimeClient({"browser": {"proxy_token": ""}})

	client.health()

	assert "headers" not in http_get.call_args.kwargs


@patch("bosshunter.browser.client.httpx.get")
def test_runtime_client_reports_unauthorized_without_exposing_token(http_get: Mock) -> None:
	"""401 应形成可诊断状态，但不得包含原始 Token。"""
	http_get.return_value = Mock(status_code=401)
	client = RuntimeClient({"browser": {"proxy_token": "top-secret"}})

	assert client.targets() == []
	assert client.last_error == "browser_runtime_unauthorized"
	assert "top-secret" not in client.last_error
