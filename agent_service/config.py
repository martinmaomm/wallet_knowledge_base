from __future__ import annotations

from pathlib import Path
from urllib.parse import SplitResult, urlsplit

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator


LOCAL_OLLAMA_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _parse_http_url(value: str, field_name: str) -> SplitResult:
    parts = urlsplit(value.strip())
    if parts.scheme.lower() not in {"http", "https"}:
        raise ValueError(f"{field_name} must use http or https")
    if not parts.hostname:
        raise ValueError(f"{field_name} must include a hostname")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"{field_name} must not include userinfo")
    try:
        parts.port
    except ValueError as exc:
        raise ValueError(f"{field_name} has an invalid port") from exc
    return parts


def _origin_from_parts(parts: SplitResult) -> str:
    hostname = parts.hostname or ""
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    port = f":{parts.port}" if parts.port is not None else ""
    return f"{parts.scheme.lower()}://{rendered_host.lower()}{port}"


def _normalize_origin(value: str) -> str:
    parts = _parse_http_url(value, "allowed_test_origins")
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise ValueError(
            "allowed_test_origins entries must be pure origins without "
            "path, query, or fragment"
        )
    return _origin_from_parts(parts)


class Settings(BaseModel):
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3.5:9b"
    bug_service_url: str = "http://localhost:8765"
    test_base_url: str
    allowed_test_origins: list[str]
    agent_db_path: Path = Path("data/agent.sqlite3")
    artifacts_dir: Path = Path("artifacts")
    source_paths: list[Path] = Field(default_factory=list)
    playwright_storage_state: Path = Path("playwright/.auth/wallet.json")
    test_payer_account: SecretStr = SecretStr("")
    test_recipient_account: SecretStr = SecretStr("")
    test_transaction_password: SecretStr = SecretStr("")
    agent_api_token: SecretStr = SecretStr("")
    model_retry_limit: int = 2
    environment_retry_limit: int = 1

    @field_validator("ollama_base_url")
    @classmethod
    def ollama_endpoint_must_be_local(cls, value: str) -> str:
        parts = _parse_http_url(value, "ollama_base_url")
        if parts.hostname not in LOCAL_OLLAMA_HOSTS:
            raise ValueError("ollama_base_url must use a local hostname")
        return value.strip().rstrip("/")

    @field_validator("allowed_test_origins")
    @classmethod
    def origins_must_be_explicit(cls, value: list[str]) -> list[str]:
        normalized = [_normalize_origin(item) for item in value if item.strip()]
        if not normalized:
            raise ValueError("allowed_test_origins cannot be empty")
        return normalized

    @field_validator("playwright_storage_state")
    @classmethod
    def storage_state_must_be_in_ignored_directory(cls, value: Path) -> Path:
        if value.is_absolute() or ".." in value.parts:
            raise ValueError(
                "playwright_storage_state must be a relative path under "
                "playwright/.auth"
            )
        if value.parts[:2] != ("playwright", ".auth") or len(value.parts) < 3:
            raise ValueError(
                "playwright_storage_state must be a relative path under "
                "playwright/.auth"
            )
        return value

    @model_validator(mode="after")
    def test_url_must_use_allowed_origin(self) -> "Settings":
        origin = self.test_origin
        if origin not in self.allowed_test_origins:
            raise ValueError(
                f"TEST_BASE_URL origin {origin!r} is not in ALLOWED_TEST_ORIGINS"
            )
        return self

    @property
    def test_origin(self) -> str:
        return _origin_from_parts(_parse_http_url(self.test_base_url, "test_base_url"))

    def assert_safe_url(self, url: str) -> None:
        origin = _origin_from_parts(_parse_http_url(url, "url"))
        if origin not in self.allowed_test_origins:
            raise ValueError(f"URL origin {origin!r} is not allowlisted")


def load_settings(env_file: str | Path = ".env") -> Settings:
    from dotenv import dotenv_values

    values = dotenv_values(env_file)
    origins = [
        item.strip().rstrip("/")
        for item in str(values.get("ALLOWED_TEST_ORIGINS") or "").split(",")
        if item.strip()
    ]
    sources = [
        Path(item.strip())
        for item in str(values.get("AGENT_SOURCE_PATHS") or "").split(",")
        if item.strip()
    ]
    return Settings(
        ollama_base_url=str(
            values.get("OLLAMA_BASE_URL") or "http://127.0.0.1:11434"
        ),
        ollama_model=str(values.get("OLLAMA_MODEL") or "qwen3.5:9b"),
        bug_service_url=str(values.get("BUG_SERVICE_URL") or "http://localhost:8765"),
        test_base_url=str(values.get("TEST_BASE_URL") or ""),
        allowed_test_origins=origins,
        agent_db_path=Path(str(values.get("AGENT_DB_PATH") or "data/agent.sqlite3")),
        artifacts_dir=Path(str(values.get("ARTIFACTS_DIR") or "artifacts")),
        source_paths=sources,
        playwright_storage_state=Path(
            str(values.get("PLAYWRIGHT_STORAGE_STATE") or "playwright/.auth/wallet.json")
        ),
        test_payer_account=str(values.get("TEST_PAYER_ACCOUNT") or ""),
        test_recipient_account=str(values.get("TEST_RECIPIENT_ACCOUNT") or ""),
        test_transaction_password=str(
            values.get("TEST_TRANSACTION_PASSWORD") or ""
        ),
        agent_api_token=str(values.get("AGENT_API_TOKEN") or ""),
    )
