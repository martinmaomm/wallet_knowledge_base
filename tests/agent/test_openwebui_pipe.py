from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from openwebui_tools.ai_test_agent import Pipe


TOKEN = "pipe-test-secret"


def make_pipe(handler) -> Pipe:
    pipe = Pipe()
    pipe.valves.AGENT_API_TOKEN = TOKEN
    pipe.client_factory = lambda **kwargs: httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        **kwargs,
    )
    return pipe


def test_pipe_forwards_validated_chat_id_prompt_and_bearer_token() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["authorization"] = request.headers["Authorization"]
        return httpx.Response(200, json={"message": "等待审批"})

    pipe = make_pipe(handler)
    result = asyncio.run(
        pipe.pipe(
            {"messages": [{"role": "user", "content": "旧消息"}]},
            __metadata__={"user_prompt": "测试内部转账"},
            __chat_id__="chat-1",
        )
    )

    assert result == "等待审批"
    assert captured == {
        "url": "http://host.docker.internal:8770/agent/messages",
        "body": {
            "thread_id": "chat-1",
            "message": "测试内部转账",
        },
        "authorization": f"Bearer {TOKEN}",
    }
    assert TOKEN not in repr(pipe.valves)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://example.com:8770",
        "https://localhost:8770",
        "http://host.docker.internal.evil:8770",
        "http://evilhost.docker.internal:8770",
        "http://localhost:9999",
        "http://[::1]:8770",
        "http://127.0.0.1:8770/path",
        "http://user:pass@127.0.0.1:8770",
        "http://127.0.0.1:8770?next=http://evil.test",
    ],
)
def test_pipe_rejects_nonlocal_or_nonorigin_base_url(base_url: str) -> None:
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"message": "unexpected"})

    pipe = make_pipe(handler)
    pipe.valves.AGENT_BASE_URL = base_url
    result = asyncio.run(
        pipe.pipe(
            {"messages": [{"role": "user", "content": "测试"}]},
            __chat_id__="chat-1",
        )
    )

    assert result == "测试 Agent 地址配置无效，仅允许本机地址。"
    assert called is False
    assert base_url not in result


def test_pipe_accepts_exact_docker_desktop_host_gateway() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"message": "ok"})

    pipe = make_pipe(handler)

    result = asyncio.run(
        pipe.pipe(
            {"messages": [{"role": "user", "content": "测试"}]},
            __chat_id__="chat-docker",
        )
    )

    assert result == "ok"
    assert captured["url"] == (
        "http://host.docker.internal:8770/agent/messages"
    )


def test_pipe_accepts_localhost_on_the_fixed_agent_port() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"message": "ok"})

    pipe = make_pipe(handler)
    pipe.valves.AGENT_BASE_URL = "http://localhost:8770"

    result = asyncio.run(
        pipe.pipe(
            {"messages": [{"role": "user", "content": "测试"}]},
            __chat_id__="chat-localhost",
        )
    )

    assert result == "ok"
    assert captured["url"] == "http://localhost:8770/agent/messages"


def test_pipe_valves_are_json_persistable_and_repr_redacts_token() -> None:
    pipe = Pipe()

    pipe.valves.AGENT_API_TOKEN = TOKEN
    dumped = pipe.valves.model_dump()
    schema = pipe.Valves.model_json_schema()

    assert json.loads(json.dumps(dumped))["AGENT_API_TOKEN"] == TOKEN
    assert TOKEN not in repr(pipe.valves)
    token_schema = schema["properties"]["AGENT_API_TOKEN"]
    assert token_schema["format"] == "password"
    assert token_schema["writeOnly"] is True


@pytest.mark.parametrize(
    "chat_id",
    [None, "", "-leading", "bad/thread", "x" * 129],
)
def test_pipe_rejects_missing_or_invalid_chat_id(
    chat_id: str | None,
) -> None:
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"message": "unexpected"})

    pipe = make_pipe(handler)
    result = asyncio.run(
        pipe.pipe(
            {"messages": [{"role": "user", "content": "测试"}]},
            __chat_id__=chat_id,
        )
    )

    assert result == "无法获取有效的 Open WebUI 会话 ID。"
    assert called is False


def test_pipe_uses_last_user_message_when_metadata_prompt_is_absent() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"message": "已接收"})

    pipe = make_pipe(handler)
    result = asyncio.run(
        pipe.pipe(
            {
                "messages": [
                    {"role": "assistant", "content": "上一条回复"},
                    {"role": "user", "content": "取消"},
                ]
            },
            __metadata__={"chat_id": "metadata-chat"},
        )
    )

    assert result == "已接收"
    assert captured == {
        "thread_id": "metadata-chat",
        "message": "取消",
    }


def test_pipe_rejects_missing_token_or_message_without_network() -> None:
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"message": "unexpected"})

    pipe = make_pipe(handler)
    pipe.valves.AGENT_API_TOKEN = ""
    no_token = asyncio.run(
        pipe.pipe(
            {"messages": [{"role": "user", "content": "测试"}]},
            __chat_id__="chat-1",
        )
    )
    pipe.valves.AGENT_API_TOKEN = TOKEN
    no_message = asyncio.run(
        pipe.pipe({"messages": []}, __chat_id__="chat-1")
    )

    assert no_token == "测试 Agent Token 尚未配置。"
    assert no_message == "未找到可发送给测试 Agent 的用户消息。"
    assert called is False


@pytest.mark.parametrize("token", ["令牌", "invalid token", "\ttoken"])
def test_pipe_rejects_invalid_bearer_token_without_network(
    token: str,
) -> None:
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"message": "unexpected"})

    pipe = make_pipe(handler)
    pipe.valves.AGENT_API_TOKEN = token
    result = asyncio.run(
        pipe.pipe(
            {"messages": [{"role": "user", "content": "测试"}]},
            __chat_id__="chat-invalid-token",
        )
    )

    assert result == "测试 Agent Token 配置无效，仅允许 ASCII Bearer Token。"
    assert token not in result
    assert called is False


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            httpx.Response(
                401,
                text=f"invalid token {TOKEN}",
            ),
            "测试 Agent 拒绝了请求（HTTP 401）。",
        ),
        (
            httpx.Response(
                503,
                text=f"database failed at /secret/path with {TOKEN}",
            ),
            "测试 Agent 服务暂时不可用（HTTP 503）。",
        ),
        (
            httpx.Response(200, text=f"not-json-{TOKEN}"),
            "测试 Agent 返回了无效响应。",
        ),
        (
            httpx.Response(200, json={"status": f"secret-{TOKEN}"}),
            "测试 Agent 返回了无效响应。",
        ),
    ],
)
def test_pipe_http_and_response_errors_are_redacted(
    response: httpx.Response,
    expected: str,
) -> None:
    pipe = make_pipe(lambda _: response)

    result = asyncio.run(
        pipe.pipe(
            {"messages": [{"role": "user", "content": "测试"}]},
            __chat_id__="chat-1",
        )
    )

    assert result == expected
    assert TOKEN not in result
    assert "/secret/path" not in result


def test_pipe_rejects_redirect_without_following_location() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            302,
            headers={
                "Location": "https://evil.test/steal",
                "X-Secret": TOKEN,
            },
        )

    pipe = make_pipe(handler)
    result = asyncio.run(
        pipe.pipe(
            {"messages": [{"role": "user", "content": "测试"}]},
            __chat_id__="chat-redirect",
        )
    )

    assert result == "测试 Agent 返回了不允许的重定向响应（HTTP 302）。"
    assert len(requests) == 1
    assert "evil.test" not in result
    assert TOKEN not in result


def test_pipe_disables_redirects_and_environment_proxy_explicitly() -> None:
    client_options: dict[str, object] = {}

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": "ok"})

    def client_factory(**kwargs):
        client_options.update(kwargs)
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            **kwargs,
        )

    pipe = Pipe()
    pipe.valves.AGENT_API_TOKEN = TOKEN
    pipe.client_factory = client_factory

    result = asyncio.run(
        pipe.pipe(
            {"messages": [{"role": "user", "content": "测试"}]},
            __chat_id__="chat-options",
        )
    )

    assert result == "ok"
    assert client_options["follow_redirects"] is False
    assert client_options["trust_env"] is False


@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        (
            httpx.ReadTimeout(
                f"timeout with {TOKEN}",
                request=httpx.Request("POST", "http://127.0.0.1"),
            ),
            "测试 Agent 请求超时，请稍后重试。",
        ),
        (
            httpx.ConnectError(
                f"connect failed with {TOKEN}",
                request=httpx.Request("POST", "http://127.0.0.1"),
            ),
            "无法连接本地测试 Agent，请确认服务已启动。",
        ),
    ],
)
def test_pipe_network_errors_are_redacted(
    exception: Exception,
    expected: str,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise exception

    pipe = make_pipe(handler)
    result = asyncio.run(
        pipe.pipe(
            {"messages": [{"role": "user", "content": "测试"}]},
            __chat_id__="chat-1",
        )
    )

    assert result == expected
    assert TOKEN not in result
