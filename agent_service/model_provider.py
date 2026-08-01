from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from typing import Any, Protocol, TypeVar
from urllib.parse import urlsplit

import httpx
from langchain_core.exceptions import OutputParserException
from ollama import ResponseError
from pydantic import BaseModel, ValidationError


T = TypeVar("T", bound=BaseModel)
LOCAL_OLLAMA_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
OLLAMA_SCHEMES = frozenset({"http", "https"})


class StructuredModelError(RuntimeError):
    """Raised when a provider cannot produce the requested strict schema."""


class ModelProvider(Protocol):
    async def generate_structured(
        self,
        *,
        task_type: str,
        prompt: str,
        schema: type[T],
    ) -> T: ...


def _validate_retry_limit(retry_limit: int) -> None:
    if (
        isinstance(retry_limit, bool)
        or not isinstance(retry_limit, int)
        or retry_limit < 0
    ):
        raise ValueError("retry_limit must be a non-negative integer")


def _validate_model(model: str) -> str:
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model must be non-blank")
    return model.strip()


def _validate_temperature(temperature: float) -> None:
    if (
        isinstance(temperature, bool)
        or not isinstance(temperature, (int, float))
        or not math.isfinite(temperature)
        or not 0 <= temperature <= 2
    ):
        raise ValueError("temperature must be a finite number between 0 and 2")


def _validate_timeout_seconds(timeout_seconds: float) -> None:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be a finite positive number")


def _is_retryable_model_error(exc: Exception) -> bool:
    if isinstance(exc, ResponseError):
        return exc.status_code == -1 or exc.status_code >= 500
    return isinstance(
        exc,
        (
            asyncio.TimeoutError,
            ValidationError,
            httpx.TimeoutException,
            httpx.TransportError,
            OutputParserException,
        ),
    )


class FakeModelProvider:
    def __init__(
        self,
        outputs: dict[str, Any],
        retry_limit: int = 2,
    ) -> None:
        _validate_retry_limit(retry_limit)
        self.outputs = outputs
        self.retry_limit = retry_limit
        self.calls: list[str] = []

    @classmethod
    def from_fixture(
        cls,
        path: Path,
        retry_limit: int = 2,
    ) -> FakeModelProvider:
        try:
            outputs = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise StructuredModelError(
                f"unable to load model fixture {path.name!r}"
            ) from exc
        if not isinstance(outputs, dict):
            raise StructuredModelError(
                f"model fixture {path.name!r} must contain a JSON object"
            )
        return cls(outputs, retry_limit=retry_limit)

    async def generate_structured(
        self,
        *,
        task_type: str,
        prompt: str,
        schema: type[T],
    ) -> T:
        del prompt
        self.calls.append(task_type)
        if task_type not in self.outputs:
            raise StructuredModelError(
                f"missing output for task_type {task_type!r}"
            )

        output = self.outputs[task_type]
        for attempt in range(self.retry_limit + 1):
            if attempt:
                self.calls.append(task_type)
            try:
                return schema.model_validate(output)
            except ValidationError:
                continue

        raise StructuredModelError(
            f"{task_type} failed structured validation after "
            f"{self.retry_limit + 1} attempts"
        )


def _validate_local_ollama_url(base_url: str) -> None:
    if not isinstance(base_url, str) or base_url != base_url.strip():
        raise ValueError("base_url must be a local Ollama endpoint")

    try:
        parsed = urlsplit(base_url)
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise ValueError(
            "base_url must be a local Ollama endpoint"
        ) from exc

    has_userinfo = parsed.username is not None or parsed.password is not None
    has_non_origin_parts = (
        parsed.path not in {"", "/"}
        or bool(parsed.query)
        or bool(parsed.fragment)
    )
    if (
        parsed.scheme not in OLLAMA_SCHEMES
        or hostname not in LOCAL_OLLAMA_HOSTS
        or not parsed.netloc
        or has_userinfo
        or has_non_origin_parts
    ):
        raise ValueError("base_url must be a local Ollama endpoint")


class OllamaProvider:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        retry_limit: int = 2,
        temperature: float = 0.0,
        timeout_seconds: float = 300,
    ) -> None:
        _validate_retry_limit(retry_limit)
        _validate_local_ollama_url(base_url)
        validated_model = _validate_model(model)
        _validate_temperature(temperature)
        _validate_timeout_seconds(timeout_seconds)

        from langchain_ollama import ChatOllama

        self.model = ChatOllama(
            base_url=base_url,
            model=validated_model,
            temperature=temperature,
            num_ctx=16384,
        )
        self.retry_limit = retry_limit
        self.timeout_seconds = timeout_seconds
        self.calls: list[str] = []

    async def generate_structured(
        self,
        *,
        task_type: str,
        prompt: str,
        schema: type[T],
    ) -> T:
        current_prompt = prompt
        failed_with_non_retryable_error = False
        for _ in range(self.retry_limit + 1):
            self.calls.append(task_type)
            try:
                runnable = self.model.with_structured_output(schema)
                async with asyncio.timeout(self.timeout_seconds):
                    result = await runnable.ainvoke(current_prompt)
                if isinstance(result, schema):
                    return result
                return schema.model_validate(result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not _is_retryable_model_error(exc):
                    failed_with_non_retryable_error = True
                    break
                current_prompt = (
                    f"{prompt}\n\nThe previous response did not match the "
                    "required schema. Return only data matching this JSON "
                    f"Schema: {schema.model_json_schema()}."
                )

        if failed_with_non_retryable_error:
            raise StructuredModelError(
                f"{task_type} failed with a non-retryable model error"
            ) from None

        raise StructuredModelError(
            f"{task_type} failed structured generation after "
            f"{self.retry_limit + 1} attempts"
        ) from None
