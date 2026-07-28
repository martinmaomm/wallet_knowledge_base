from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from dotenv import dotenv_values
from requests import Response
from requests.exceptions import ChunkedEncodingError, ConnectionError, RequestException, Timeout

from .models import BugRecord
from .normalize import normalize_bug


@dataclass(frozen=True)
class ZenTaoConfig:
    base_url: str
    account: str
    password: str
    product_id: int = 9
    product_name: str = "内部钱包"
    status: str = "all"
    timeout: int = 20
    page_limit: int = 200
    retries: int = 3

    @classmethod
    def from_env_file(
        cls,
        env_file: str | Path,
        *,
        product_id: int = 9,
        product_name: str = "内部钱包",
    ) -> "ZenTaoConfig":
        values = dotenv_values(env_file)
        base_url = str(values.get("ZENTAO_BASE_URL") or "").strip()
        account = str(values.get("ZENTAO_ACCOUNT") or "").strip()
        password = str(values.get("ZENTAO_PASSWORD") or "").strip()
        if not base_url or not account or not password:
            raise RuntimeError("env file must define ZENTAO_BASE_URL, ZENTAO_ACCOUNT and ZENTAO_PASSWORD")
        return cls(
            base_url=base_url,
            account=account,
            password=password,
            product_id=max(1, int(product_id)),
            product_name=product_name.strip() or "内部钱包",
        )


class ZenTaoClient:
    def __init__(self, config: ZenTaoConfig):
        self.config = config
        self.session = requests.Session()

    def login(self) -> None:
        base = self.config.base_url.rstrip("/")
        session_response = self._request("GET", f"{base}/index.php?m=api&f=getSessionID&t=json")
        session_response.raise_for_status()
        outer = session_response.json()
        info = json.loads(outer["data"]) if isinstance(outer.get("data"), str) else outer.get("data", {})

        session_id = str(info["sessionID"])
        session_name = str(info.get("sessionName") or "zentaosid")
        host = urlparse(base).hostname
        if host:
            self.session.cookies.set(session_name, session_id, domain=host, path="/")

        password_md5 = hashlib.md5(self.config.password.encode("utf-8")).hexdigest()
        encrypted = hashlib.md5((password_md5 + str(info["rand"])).encode("utf-8")).hexdigest()
        login_response = self._request(
            "POST",
            f"{base}/index.php?m=user&f=login&zentaosid={session_id}",
            data={
                "account": self.config.account,
                "password": encrypted,
                "verifyRand": info["rand"],
                "keepLogin": "on",
                "referer": "/",
            },
            allow_redirects=True,
        )
        login_response.raise_for_status()

    def fetch_records(self) -> list[BugRecord]:
        bugs = self.fetch_product_bugs()
        module_names = self.fetch_module_names(bugs)
        return [
            normalize_bug(
                bug,
                default_product=self.config.product_name,
                module_names=module_names,
            )
            for bug in bugs
        ]

    def fetch_product_bugs(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        page = 1
        total: int | None = None
        while True:
            obj = self._get_json(
                f"/api.php/v1/products/{self.config.product_id}/bugs",
                {"page": page, "limit": self.config.page_limit, "status": self.config.status},
            )
            bugs = obj.get("bugs", [])
            if not isinstance(bugs, list):
                raise RuntimeError("ZenTao response field 'bugs' is not a list")
            rows.extend(item for item in bugs if isinstance(item, dict))

            total_value = obj.get("total")
            if isinstance(total_value, int):
                total = total_value
            elif isinstance(total_value, str) and total_value.isdigit():
                total = int(total_value)
            if not bugs or (total is not None and len(rows) >= total):
                break
            page += 1
        return rows

    def fetch_module_names(self, bugs: list[dict[str, Any]]) -> dict[int, str]:
        first_bug_by_module: dict[int, int] = {}
        for bug in bugs:
            module_id = int(bug.get("module") or 0)
            bug_id = int(bug.get("id") or 0)
            if module_id > 0 and bug_id > 0:
                first_bug_by_module.setdefault(module_id, bug_id)

        names: dict[int, str] = {0: "未设置"}
        for module_id, bug_id in first_bug_by_module.items():
            detail = self._get_json(f"/api.php/v1/bugs/{bug_id}")
            title = str(detail.get("moduleTitle") or "").strip()
            names[module_id] = title or f"模块ID {module_id}"
        return names

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._request("GET", f"{self.config.base_url.rstrip('/')}{path}", params=params)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise RuntimeError(f"ZenTao returned non-object JSON for {path}")
        return value

    def _request(self, method: str, url: str, **kwargs: Any) -> Response:
        last_error: RequestException | None = None
        timeout = kwargs.pop("timeout", self.config.timeout)
        for attempt in range(1, self.config.retries + 1):
            try:
                return self.session.request(method, url, timeout=timeout, **kwargs)
            except (ChunkedEncodingError, ConnectionError, Timeout) as exc:
                last_error = exc
                if attempt >= self.config.retries:
                    raise
                time.sleep(min(attempt, 3))
        if last_error:
            raise last_error
        raise RuntimeError(f"Request failed without a captured exception: {method}")

