from __future__ import annotations

import argparse
import json
import os
import re
from typing import Any
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv


THREAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._~+/-]+=*$")
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1"})
AGENT_PORT = 8770


def validate_agent_url(value: str) -> str:
    parts = urlsplit(value.strip())
    if (
        parts.scheme != "http"
        or parts.hostname not in LOCAL_HOSTS
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        raise ValueError("AGENT_BASE_URL must be a local HTTP origin")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("AGENT_BASE_URL has an invalid port") from exc
    if port != AGENT_PORT:
        raise ValueError(f"AGENT_BASE_URL must use local port {AGENT_PORT}")
    return value.strip().rstrip("/")


class AgentClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = validate_agent_url(base_url)
        if (
            not isinstance(token, str)
            or len(token) > 4096
            or TOKEN_PATTERN.fullmatch(token) is None
        ):
            raise ValueError("AGENT_API_TOKEN must be a valid Bearer token")
        self._token = token
        self._transport = transport

    def send(self, thread_id: str, message: str) -> dict[str, Any]:
        if THREAD_ID_PATTERN.fullmatch(thread_id) is None:
            raise ValueError("thread_id is invalid")
        with httpx.Client(
            transport=self._transport,
            timeout=600,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            response = client.post(
                f"{self.base_url}/agent/messages",
                json={"thread_id": thread_id, "message": message},
                headers={"Authorization": f"Bearer {self._token}"},
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Agent returned an invalid response")
        return payload


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Run the local Web2 internal-transfer Agent demo."
    )
    parser.add_argument("--thread-id", default="portfolio-demo")
    parser.add_argument("--approve", action="store_true")
    args = parser.parse_args()
    client = AgentClient(
        base_url=os.getenv("AGENT_BASE_URL", "http://127.0.0.1:8770"),
        token=os.getenv("AGENT_API_TOKEN", ""),
    )
    result = client.send(
        args.thread_id,
        "批准" if args.approve else "测试 Web2 内部转账",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
