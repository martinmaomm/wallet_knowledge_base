from __future__ import annotations

import asyncio
import json
import math
import sys
import types
from pathlib import Path

import httpx
import ollama
import pytest
from langchain_core.exceptions import OutputParserException
from pydantic import BaseModel, ConfigDict, SecretStr

from agent_service.dsl import validate_test_plan
from agent_service.model_provider import (
    FakeModelProvider,
    OllamaProvider,
    StructuredModelError,
)
from agent_service.schemas import (
    RequirementSet,
    RiskAnalysis,
    TestPlan as GeneratedPlanSchema,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "model_outputs.json"


class CredentialBearingOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: SecretStr


def _exception_chain_text(exc: BaseException) -> str:
    messages: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        messages.extend((str(current), repr(current)))
        current = current.__cause__ or current.__context__
    return "\n".join(messages)


def test_fake_provider_returns_strictly_validated_schema() -> None:
    provider = FakeModelProvider.from_fixture(FIXTURE_PATH)

    result = asyncio.run(
        provider.generate_structured(
            task_type="extract_requirements",
            prompt="extract",
            schema=RequirementSet,
        )
    )

    assert isinstance(result, RequirementSet)
    assert result.requirements[0].confirmed is True
    assert result.requirements[0].source_refs == ["{{SOURCE_ID}}"]
    assert provider.calls == ["extract_requirements"]


def test_fake_provider_retries_twice_after_initial_attempt() -> None:
    provider = FakeModelProvider(
        {
            "extract_requirements": {
                "scope": "web2_internal_transfer",
                "requirements": [{"unexpected": "credential-secret"}],
                "missing_rules": [],
            }
        },
        retry_limit=2,
    )

    with pytest.raises(StructuredModelError, match="3 attempts") as exc_info:
        asyncio.run(
            provider.generate_structured(
                task_type="extract_requirements",
                prompt="do not expose prompt-secret",
                schema=RequirementSet,
            )
        )

    assert provider.calls == ["extract_requirements"] * 3
    assert "credential-secret" not in str(exc_info.value)
    assert "prompt-secret" not in str(exc_info.value)


def test_fake_provider_reports_missing_task_type_without_key_error() -> None:
    provider = FakeModelProvider(
        {
            "credentials": {
                "token": "fixture-secret",
            }
        }
    )

    with pytest.raises(
        StructuredModelError,
        match=r"missing output for task_type 'extract_requirements'",
    ) as exc_info:
        asyncio.run(
            provider.generate_structured(
                task_type="extract_requirements",
                prompt="extract",
                schema=RequirementSet,
            )
        )

    assert "fixture-secret" not in str(exc_info.value)
    assert provider.calls == ["extract_requirements"]


def test_fake_provider_does_not_expose_secret_validation_input() -> None:
    provider = FakeModelProvider(
        {
            "secret_output": {
                "token": "model-secret",
                "unexpected": "prompt-secret",
            }
        },
        retry_limit=0,
    )

    with pytest.raises(StructuredModelError) as exc_info:
        asyncio.run(
            provider.generate_structured(
                task_type="secret_output",
                prompt="prompt-secret",
                schema=CredentialBearingOutput,
            )
        )

    error_message = str(exc_info.value)
    assert "model-secret" not in error_message
    assert "prompt-secret" not in error_message


def test_fixture_test_plan_satisfies_current_golden_contract() -> None:
    provider = FakeModelProvider.from_fixture(FIXTURE_PATH)

    plan = asyncio.run(
        provider.generate_structured(
            task_type="generate_test_plan",
            prompt="generate",
            schema=GeneratedPlanSchema,
        )
    )

    assert validate_test_plan(plan, require_golden_set=True) == plan


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost.example.com:11434",
        "http://user:password@localhost:11434",
        "http://192.168.1.10:11434",
        "https://ollama.example.com",
        "ftp://localhost:11434",
        "localhost:11434",
    ],
)
def test_ollama_provider_rejects_non_local_or_unsafe_urls(
    base_url: str,
) -> None:
    with pytest.raises(ValueError, match="local Ollama endpoint"):
        OllamaProvider(base_url=base_url, model="qwen3.5:9b")


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:11434",
        "https://127.0.0.1:11434",
        "http://[::1]:11434",
    ],
)
def test_ollama_provider_accepts_exact_local_hosts_without_network(
    base_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[dict[str, object]] = []

    class StubChatOllama:
        def __init__(self, **kwargs: object) -> None:
            created.append(kwargs)

    monkeypatch.setitem(
        sys.modules,
        "langchain_ollama",
        types.SimpleNamespace(ChatOllama=StubChatOllama),
    )

    provider = OllamaProvider(base_url=base_url, model="qwen3.5:9b")

    assert provider.retry_limit == 2
    assert provider.timeout_seconds == 300
    assert created == [
        {
            "base_url": base_url,
            "model": "qwen3.5:9b",
            "temperature": 0.0,
            "num_ctx": 16384,
        }
    ]


def test_ollama_provider_retries_transient_failure_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    class RecoveringRunnable:
        async def ainvoke(self, prompt: str) -> dict[str, object]:
            nonlocal attempts
            del prompt
            attempts += 1
            if attempts == 1:
                raise httpx.ConnectError("model-secret")
            return {
                "scope": "web2_internal_transfer",
                "requirements": [],
                "missing_rules": [],
            }

    class StubChatOllama:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def with_structured_output(
            self,
            schema: type[BaseModel],
        ) -> RecoveringRunnable:
            del schema
            return RecoveringRunnable()

    monkeypatch.setitem(
        sys.modules,
        "langchain_ollama",
        types.SimpleNamespace(ChatOllama=StubChatOllama),
    )
    provider = OllamaProvider(
        base_url="http://127.0.0.1:11434",
        model="qwen3.5:9b",
        retry_limit=2,
    )

    result = asyncio.run(
        provider.generate_structured(
            task_type="extract_requirements",
            prompt="prompt-secret",
            schema=RequirementSet,
        )
    )

    assert result.scope == "web2_internal_transfer"
    assert attempts == 2
    assert provider.calls == ["extract_requirements"] * 2


def test_ollama_provider_retries_stream_response_error_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    class RecoveringRunnable:
        async def ainvoke(self, prompt: str) -> dict[str, object]:
            nonlocal attempts
            del prompt
            attempts += 1
            if attempts == 1:
                raise ollama.ResponseError(
                    "stream-runtime-secret",
                    status_code=-1,
                )
            return {
                "scope": "web2_internal_transfer",
                "requirements": [],
                "missing_rules": [],
            }

    class StubChatOllama:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def with_structured_output(
            self,
            schema: type[BaseModel],
        ) -> RecoveringRunnable:
            del schema
            return RecoveringRunnable()

    monkeypatch.setitem(
        sys.modules,
        "langchain_ollama",
        types.SimpleNamespace(ChatOllama=StubChatOllama),
    )
    provider = OllamaProvider(
        base_url="http://127.0.0.1:11434",
        model="qwen3.5:9b",
        retry_limit=2,
    )

    result = asyncio.run(
        provider.generate_structured(
            task_type="extract_requirements",
            prompt="prompt-secret",
            schema=RequirementSet,
        )
    )

    assert result.scope == "web2_internal_transfer"
    assert attempts == 2
    assert provider.calls == ["extract_requirements"] * 2


def test_ollama_provider_times_out_three_times_without_leaking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HangingRunnable:
        async def ainvoke(self, prompt: str) -> None:
            del prompt
            await asyncio.sleep(1)

    class StubChatOllama:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def with_structured_output(
            self,
            schema: type[BaseModel],
        ) -> HangingRunnable:
            del schema
            return HangingRunnable()

    monkeypatch.setitem(
        sys.modules,
        "langchain_ollama",
        types.SimpleNamespace(ChatOllama=StubChatOllama),
    )
    provider = OllamaProvider(
        base_url="http://127.0.0.1:11434",
        model="qwen3.5:9b",
        retry_limit=2,
        timeout_seconds=0.01,
    )

    with pytest.raises(StructuredModelError, match="3 attempts") as exc_info:
        asyncio.run(
            provider.generate_structured(
                task_type="extract_requirements",
                prompt="prompt-secret",
                schema=RequirementSet,
            )
        )

    assert provider.calls == ["extract_requirements"] * 3
    assert exc_info.value.__cause__ is None
    assert "prompt-secret" not in str(exc_info.value)


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadTimeout("transport-secret"),
        httpx.ConnectError("transport-secret"),
        OutputParserException("parser-secret"),
        ollama.ResponseError("stream-secret", status_code=-1),
        ollama.ResponseError("ollama-secret", status_code=503),
    ],
)
def test_ollama_provider_retries_only_retryable_failures_and_redacts(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    class FailingRunnable:
        async def ainvoke(self, prompt: str) -> None:
            del prompt
            raise failure

    class StubChatOllama:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def with_structured_output(
            self,
            schema: type[BaseModel],
        ) -> FailingRunnable:
            del schema
            return FailingRunnable()

    monkeypatch.setitem(
        sys.modules,
        "langchain_ollama",
        types.SimpleNamespace(ChatOllama=StubChatOllama),
    )
    provider = OllamaProvider(
        base_url="http://127.0.0.1:11434",
        model="qwen3.5:9b",
        retry_limit=2,
    )

    with pytest.raises(StructuredModelError, match="3 attempts") as exc_info:
        asyncio.run(
            provider.generate_structured(
                task_type="extract_requirements",
                prompt="prompt-secret",
                schema=RequirementSet,
            )
        )

    assert provider.calls == ["extract_requirements"] * 3
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert "secret" not in _exception_chain_text(exc_info.value)


def test_ollama_provider_retries_pydantic_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validation_error = None
    try:
        RequirementSet.model_validate({"scope": "invalid-secret"})
    except Exception as exc:
        validation_error = exc
    assert validation_error is not None

    class FailingRunnable:
        async def ainvoke(self, prompt: str) -> None:
            del prompt
            raise validation_error

    class StubChatOllama:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def with_structured_output(
            self,
            schema: type[BaseModel],
        ) -> FailingRunnable:
            del schema
            return FailingRunnable()

    monkeypatch.setitem(
        sys.modules,
        "langchain_ollama",
        types.SimpleNamespace(ChatOllama=StubChatOllama),
    )
    provider = OllamaProvider(
        base_url="http://127.0.0.1:11434",
        model="qwen3.5:9b",
    )

    with pytest.raises(StructuredModelError) as exc_info:
        asyncio.run(
            provider.generate_structured(
                task_type="extract_requirements",
                prompt="prompt-secret",
                schema=RequirementSet,
            )
        )

    assert provider.calls == ["extract_requirements"] * 3
    assert exc_info.value.__cause__ is None
    assert "invalid-secret" not in str(exc_info.value)


@pytest.mark.parametrize(
    "failure",
    [
        TypeError("programming-secret"),
        ollama.ResponseError("client-secret", status_code=400),
        ollama.ResponseError("client-secret", status_code=499),
    ],
)
def test_ollama_provider_does_not_retry_non_retryable_failures(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    class FailingRunnable:
        async def ainvoke(self, prompt: str) -> None:
            del prompt
            raise failure

    class StubChatOllama:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def with_structured_output(
            self,
            schema: type[BaseModel],
        ) -> FailingRunnable:
            del schema
            return FailingRunnable()

    monkeypatch.setitem(
        sys.modules,
        "langchain_ollama",
        types.SimpleNamespace(ChatOllama=StubChatOllama),
    )
    provider = OllamaProvider(
        base_url="http://127.0.0.1:11434",
        model="qwen3.5:9b",
    )

    with pytest.raises(StructuredModelError, match="non-retryable") as exc_info:
        asyncio.run(
            provider.generate_structured(
                task_type="extract_requirements",
                prompt="prompt-secret",
                schema=RequirementSet,
            )
        )

    assert provider.calls == ["extract_requirements"]
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert "secret" not in _exception_chain_text(exc_info.value)


def test_ollama_provider_propagates_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CancelledRunnable:
        async def ainvoke(self, prompt: str) -> None:
            del prompt
            raise asyncio.CancelledError

    class StubChatOllama:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def with_structured_output(
            self,
            schema: type[BaseModel],
        ) -> CancelledRunnable:
            del schema
            return CancelledRunnable()

    monkeypatch.setitem(
        sys.modules,
        "langchain_ollama",
        types.SimpleNamespace(ChatOllama=StubChatOllama),
    )
    provider = OllamaProvider(
        base_url="http://127.0.0.1:11434",
        model="qwen3.5:9b",
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            provider.generate_structured(
                task_type="extract_requirements",
                prompt="prompt-secret",
                schema=RequirementSet,
            )
        )

    assert provider.calls == ["extract_requirements"]


@pytest.mark.parametrize("model", ["", " ", "\t"])
def test_ollama_provider_rejects_blank_model(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "langchain_ollama",
        types.SimpleNamespace(ChatOllama=object),
    )

    with pytest.raises(ValueError, match="model must be non-blank"):
        OllamaProvider(
            base_url="http://127.0.0.1:11434",
            model=model,
        )


@pytest.mark.parametrize(
    "temperature",
    [True, False, -0.1, 2.1, math.nan, math.inf, -math.inf],
)
def test_ollama_provider_rejects_invalid_temperature(
    monkeypatch: pytest.MonkeyPatch,
    temperature: float,
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "langchain_ollama",
        types.SimpleNamespace(ChatOllama=object),
    )

    with pytest.raises(ValueError, match="temperature"):
        OllamaProvider(
            base_url="http://127.0.0.1:11434",
            model="qwen3.5:9b",
            temperature=temperature,
        )


@pytest.mark.parametrize(
    "timeout_seconds",
    [True, False, 0, -1, math.nan, math.inf, -math.inf],
)
def test_ollama_provider_rejects_invalid_timeout(
    monkeypatch: pytest.MonkeyPatch,
    timeout_seconds: float,
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "langchain_ollama",
        types.SimpleNamespace(ChatOllama=object),
    )

    with pytest.raises(ValueError, match="timeout_seconds"):
        OllamaProvider(
            base_url="http://127.0.0.1:11434",
            model="qwen3.5:9b",
            timeout_seconds=timeout_seconds,
        )


@pytest.mark.parametrize("temperature", [0, 0.0, 2, 2.0])
def test_ollama_provider_accepts_temperature_boundaries_and_strips_model(
    monkeypatch: pytest.MonkeyPatch,
    temperature: float,
) -> None:
    created: list[dict[str, object]] = []

    class StubChatOllama:
        def __init__(self, **kwargs: object) -> None:
            created.append(kwargs)

    monkeypatch.setitem(
        sys.modules,
        "langchain_ollama",
        types.SimpleNamespace(ChatOllama=StubChatOllama),
    )

    OllamaProvider(
        base_url="http://127.0.0.1:11434",
        model=" qwen3.5:9b ",
        temperature=temperature,
        timeout_seconds=1,
    )

    assert created[0]["model"] == "qwen3.5:9b"
    assert created[0]["temperature"] == temperature


def test_fixture_analyze_risks_matches_strict_schema() -> None:
    provider = FakeModelProvider.from_fixture(FIXTURE_PATH)

    result = asyncio.run(
        provider.generate_structured(
            task_type="analyze_risks",
            prompt="analyze",
            schema=RiskAnalysis,
        )
    )

    assert isinstance(result, RiskAnalysis)
    assert result.ambiguities == []
    assert result.risks == []
    assert result.bug_queries == []


def test_fixture_keeps_classify_failure_output() -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    assert fixture["classify_failure"] == {
        "category": "product",
        "summary": "Deterministic assertion failed",
        "evidence_refs": ["execution_results.json"],
        "related_bug_ids": [],
        "recommended_action": "Review captured request and screenshot",
    }
