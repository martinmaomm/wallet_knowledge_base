from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field


LOCAL_AGENT_HOSTS = frozenset(
    {
        "host.docker.internal",
        "localhost",
        "127.0.0.1",
    }
)
LOCAL_AGENT_PORT = 8770
THREAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
BEARER_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._~+/-]+=*$")
MAX_TOKEN_LENGTH = 4096
PIPE_TIMEOUT_SECONDS = 300.0


def _validate_local_agent_origin(value: str) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise ValueError("Agent base URL must be a local HTTP origin")
    try:
        parsed = urlsplit(value)
        parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "Agent base URL must be a local HTTP origin"
        ) from exc
    if (
        parsed.scheme.lower() != "http"
        or parsed.hostname not in LOCAL_AGENT_HOSTS
        or parsed.port != LOCAL_AGENT_PORT
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or bool(parsed.query)
        or bool(parsed.fragment)
    ):
        raise ValueError("Agent base URL must be a local HTTP origin")
    return value.rstrip("/")


def _valid_thread_id(value: Any) -> str | None:
    if isinstance(value, str) and THREAD_ID_PATTERN.fullmatch(value):
        return value
    return None


def _valid_bearer_token(value: str) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_TOKEN_LENGTH
    ):
        return False
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        return False
    return BEARER_TOKEN_PATTERN.fullmatch(value) is not None


def _latest_message(body: dict[str, Any]) -> str:
    messages = body.get("messages")
    if not isinstance(messages, list):
        return ""
    for item in reversed(messages):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ""


class Pipe:
    class Valves(BaseModel):
        model_config = ConfigDict(validate_assignment=True)

        AGENT_BASE_URL: str = Field(
            default="http://host.docker.internal:8770",
            description="Local AI Test Agent origin",
        )
        AGENT_API_TOKEN: str = Field(
            default="",
            description="Bearer token from the Agent service .env",
            repr=False,
            json_schema_extra={
                "format": "password",
                "writeOnly": True,
            },
        )

    def __init__(self) -> None:
        self.valves = self.Valves()
        self.client_factory = httpx.AsyncClient

    async def pipe(
        self,
        body: dict[str, Any],
        __metadata__: dict[str, Any] | None = None,
        __chat_id__: str | None = None,
    ) -> str:
        metadata = __metadata__ if isinstance(__metadata__, dict) else {}
        chat_id = _valid_thread_id(
            __chat_id__ or metadata.get("chat_id")
        )
        if chat_id is None:
            return "无法获取有效的 Open WebUI 会话 ID。"

        try:
            base_url = _validate_local_agent_origin(
                self.valves.AGENT_BASE_URL
            )
        except ValueError:
            return "测试 Agent 地址配置无效，仅允许本机地址。"

        token = self.valves.AGENT_API_TOKEN
        if not token:
            return "测试 Agent Token 尚未配置。"
        if not _valid_bearer_token(token):
            return (
                "测试 Agent Token 配置无效，"
                "仅允许 ASCII Bearer Token。"
            )

        metadata_prompt = metadata.get("user_prompt")
        message = (
            metadata_prompt.strip()
            if isinstance(metadata_prompt, str)
            and metadata_prompt.strip()
            else _latest_message(body)
        )
        if not message:
            return "未找到可发送给测试 Agent 的用户消息。"

        try:
            async with self.client_factory(
                timeout=PIPE_TIMEOUT_SECONDS,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    f"{base_url}/agent/messages",
                    json={"thread_id": chat_id, "message": message},
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.TimeoutException:
            return "测试 Agent 请求超时，请稍后重试。"
        except httpx.RequestError:
            return "无法连接本地测试 Agent，请确认服务已启动。"

        if 300 <= response.status_code < 400:
            return (
                "测试 Agent 返回了不允许的重定向响应"
                f"（HTTP {response.status_code}）。"
            )
        if 400 <= response.status_code < 500:
            return f"测试 Agent 拒绝了请求（HTTP {response.status_code}）。"
        if response.status_code >= 500:
            return (
                "测试 Agent 服务暂时不可用"
                f"（HTTP {response.status_code}）。"
            )
        if not 200 <= response.status_code < 300:
            return (
                "测试 Agent 返回了无效协议响应"
                f"（HTTP {response.status_code}）。"
            )

        try:
            payload = response.json()
        except (ValueError, UnicodeDecodeError):
            return "测试 Agent 返回了无效响应。"
        result = payload.get("message") if isinstance(payload, dict) else None
        if not isinstance(result, str) or not result:
            return "测试 Agent 返回了无效响应。"
        return result
