from pathlib import Path

import pytest

from agent_service.config import Settings, load_settings


def settings_kwargs(tmp_path: Path) -> dict[str, object]:
    return {
        "ollama_base_url": "http://localhost:11434",
        "ollama_model": "qwen3.5:9b",
        "bug_service_url": "http://localhost:8765",
        "test_base_url": "https://wallet-test.local/internal-transfer",
        "allowed_test_origins": ["https://wallet-test.local/"],
        "agent_db_path": tmp_path / "agent.sqlite3",
        "artifacts_dir": tmp_path / "artifacts",
        "source_paths": [],
        "playwright_storage_state": Path("playwright/.auth/wallet.json"),
    }


def test_settings_accept_only_configured_test_origin(tmp_path: Path) -> None:
    settings = Settings(**settings_kwargs(tmp_path))

    assert settings.allowed_test_origins == ["https://wallet-test.local"]
    assert settings.test_origin == "https://wallet-test.local"
    settings.assert_safe_url("https://wallet-test.local/transfer")
    with pytest.raises(ValueError, match="not allowlisted"):
        settings.assert_safe_url("https://wallet.example.com/transfer")


@pytest.mark.parametrize(
    "ollama_base_url",
    [
        "ftp://localhost:11434",
        "http://localhost.example.com:11434",
        "https://ollama.example.com",
        "http://user:password@localhost:11434",
    ],
)
def test_settings_reject_non_local_ollama_endpoint(
    tmp_path: Path, ollama_base_url: str
) -> None:
    values = settings_kwargs(tmp_path)
    values["ollama_base_url"] = ollama_base_url

    with pytest.raises(ValueError, match="ollama_base_url"):
        Settings(**values)


@pytest.mark.parametrize(
    "ollama_base_url",
    [
        "http://localhost:11434",
        "https://127.0.0.1:11434",
        "http://[::1]:11434",
    ],
)
def test_settings_accept_local_ollama_endpoint(
    tmp_path: Path, ollama_base_url: str
) -> None:
    values = settings_kwargs(tmp_path)
    values["ollama_base_url"] = ollama_base_url

    assert Settings(**values).ollama_base_url == ollama_base_url


@pytest.mark.parametrize(
    "origin",
    [
        "ftp://wallet-test.local",
        "https://wallet-test.local/path",
        "https://wallet-test.local?debug=1",
        "https://wallet-test.local#fragment",
        "https://user@wallet-test.local",
        "wallet-test.local",
    ],
)
def test_settings_reject_invalid_allowed_test_origin(
    tmp_path: Path, origin: str
) -> None:
    values = settings_kwargs(tmp_path)
    values["allowed_test_origins"] = [origin]

    with pytest.raises(ValueError, match="allowed_test_origins"):
        Settings(**values)


def test_settings_reject_test_base_url_outside_allowlist(tmp_path: Path) -> None:
    values = settings_kwargs(tmp_path)
    values["test_base_url"] = "https://untrusted.example.com/transfer"

    with pytest.raises(ValueError, match="TEST_BASE_URL origin"):
        Settings(**values)


@pytest.mark.parametrize(
    "storage_state",
    [
        Path("/tmp/wallet.json"),
        Path("state.json"),
        Path("custom/session.json"),
        Path("playwright/.auth/../wallet.json"),
    ],
)
def test_settings_reject_storage_state_outside_auth_directory(
    tmp_path: Path, storage_state: Path
) -> None:
    values = settings_kwargs(tmp_path)
    values["playwright_storage_state"] = storage_state

    with pytest.raises(ValueError, match="playwright_storage_state"):
        Settings(**values)


def test_load_settings_reads_agent_api_token(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "TEST_BASE_URL=https://wallet-test.local/internal-transfer",
                "ALLOWED_TEST_ORIGINS=https://wallet-test.local/",
                "AGENT_API_TOKEN=local-agent-token",
            ]
        ),
        encoding="utf-8",
    )

    settings = load_settings(env_file)

    assert settings.allowed_test_origins == ["https://wallet-test.local"]
    assert settings.agent_api_token.get_secret_value() == "local-agent-token"


def test_settings_and_loader_default_to_ipv4_ollama_loopback(
    tmp_path: Path,
) -> None:
    direct_values = settings_kwargs(tmp_path)
    direct_values.pop("ollama_base_url")
    direct_settings = Settings(**direct_values)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "TEST_BASE_URL=https://wallet-test.local/internal-transfer",
                "ALLOWED_TEST_ORIGINS=https://wallet-test.local/",
            ]
        ),
        encoding="utf-8",
    )

    loaded_settings = load_settings(env_file)

    assert direct_settings.ollama_base_url == "http://127.0.0.1:11434"
    assert loaded_settings.ollama_base_url == "http://127.0.0.1:11434"


def test_agent_api_token_is_masked_in_settings_repr(tmp_path: Path) -> None:
    settings = Settings(
        **settings_kwargs(tmp_path),
        agent_api_token="local-agent-token",
    )

    assert "local-agent-token" not in repr(settings)
    assert settings.agent_api_token.get_secret_value() == "local-agent-token"
