from __future__ import annotations

import json

import httpx
import pytest

from scripts.run_internal_transfer_demo import AgentClient


def test_demo_client_sends_local_authenticated_request() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["Authorization"]
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "waiting_approval"})

    client = AgentClient(
        base_url="http://127.0.0.1:8770",
        token="test-token",
        transport=httpx.MockTransport(handler),
    )
    result = client.send("demo-1", "测试 Web2 内部转账")

    assert result["status"] == "waiting_approval"
    assert captured == {
        "url": "http://127.0.0.1:8770/agent/messages",
        "authorization": "Bearer test-token",
        "body": {
            "thread_id": "demo-1",
            "message": "测试 Web2 内部转账",
        },
    }


@pytest.mark.parametrize(
    "base_url",
    [
        "https://127.0.0.1:8770",
        "http://example.com:8770",
        "http://127.0.0.1:9999",
        "http://127.0.0.1:8770/path",
        "http://user:pass@127.0.0.1:8770",
    ],
)
def test_demo_client_rejects_unsafe_agent_url(base_url: str) -> None:
    with pytest.raises(ValueError, match="AGENT_BASE_URL"):
        AgentClient(base_url=base_url, token="test-token")


@pytest.mark.parametrize("token", ["", "has spaces", "\u5bc6\u7801"])
def test_demo_client_rejects_invalid_token(token: str) -> None:
    with pytest.raises(ValueError, match="AGENT_API_TOKEN"):
        AgentClient(base_url="http://127.0.0.1:8770", token=token)
