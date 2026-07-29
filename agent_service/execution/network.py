from __future__ import annotations

import json
import os
import re
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import (
    SplitResult,
    quote,
    quote_plus,
    unquote_plus,
    urlsplit,
)
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from pydantic import BaseModel, ConfigDict, Field


MAX_CAPTURE_BODY_BYTES = 64 * 1024
MAX_BODY_DEPTH = 8
MAX_CONTAINER_ITEMS = 100
MAX_TRACE_METADATA_BYTES = 16 * 1024 * 1024
REDACTED = "[REDACTED]"
TRUNCATED = "[TRUNCATED]"
_SAFE_TRACE_ENTRIES = frozenset({"trace.trace", "trace.stacks"})
_SAFE_TRACE_EVENT_TYPES = frozenset(
    {"context-options", "before", "after"}
)
_BEARER_PATTERN = re.compile(
    rb"(?i)\bbearer(?:\s+|%20|%2520|\+)+"
    rb"(?!\[REDACTED\])"
    rb"[A-Za-z0-9._~+/=-]+"
)
_EMBEDDED_CREDENTIAL_PATTERN = re.compile(
    r"""(?ix)
    (?:
        \bbearer\s+
        |
        ["']?\b(?:token|api[-_ ]?key|password|secret|cookie|account)\b
        ["']?\s*[:=]\s*["']?
    )
    [^\s,;}"']+
    """
)

_SENSITIVE_KEY_PARTS = frozenset(
    {
        "account",
        "apikey",
        "authorization",
        "body",
        "cookie",
        "credential",
        "headers",
        "password",
        "postdata",
        "secret",
        "session",
        "token",
        "url",
    }
)


class NetworkEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    method: str
    path: str
    status: int | None
    duration_ms: int | None = None
    request_headers: dict[str, str] = Field(default_factory=dict)
    response_headers: dict[str, str] = Field(default_factory=dict)
    request_body: object | None = None
    response_body: object | None = None


class TraceSanitizationError(RuntimeError):
    """A fixed-message boundary for unsafe or malformed trace archives."""


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


def validate_allowed_origin(value: str) -> str:
    parts = _parse_http_url(value, "allowed_origin")
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise ValueError(
            "allowed_origin must be a pure origin without path, query, or "
            "fragment"
        )
    return _origin_from_parts(parts)


def is_same_origin_request(url: str, allowed_origin: str) -> bool:
    try:
        parts = _parse_http_url(url, "request_url")
    except (TypeError, ValueError):
        return False
    return _origin_from_parts(parts) == allowed_origin


def normalized_path(url: str) -> str:
    parts = _parse_http_url(url, "request_url")
    return parts.path or "/"


def _normalized_key(key: str) -> str:
    return "".join(character for character in key.lower() if character.isalnum())


def _is_sensitive_key(key: str) -> bool:
    normalized = _normalized_key(key)
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _clean_sensitive_values(
    sensitive_values: Collection[str],
) -> tuple[str, ...]:
    variants: set[str] = set()
    for value in sensitive_values:
        if not isinstance(value, str) or not value:
            continue
        variants.add(value)
        encoded = quote(value, safe="")
        plus_encoded = quote_plus(value, safe="")
        variants.update(
            {
                encoded,
                plus_encoded,
                re.sub(
                    r"%[0-9A-Fa-f]{2}",
                    lambda match: match.group(0).lower(),
                    encoded,
                ),
                re.sub(
                    r"%[0-9A-Fa-f]{2}",
                    lambda match: match.group(0).lower(),
                    plus_encoded,
                ),
            }
        )
    return tuple(
        sorted(
            variants,
            key=len,
            reverse=True,
        )
    )


def _redact_string(value: str, sensitive_values: tuple[str, ...]) -> str:
    redacted = value
    for secret in sensitive_values:
        redacted = redacted.replace(secret, REDACTED)
    return redacted


def _redact_runtime_string(
    value: str,
    sensitive_values: tuple[str, ...],
) -> str:
    redacted = _redact_string(value, sensitive_values)
    decoded = redacted
    for _ in range(3):
        if _EMBEDDED_CREDENTIAL_PATTERN.search(decoded):
            return REDACTED
        next_decoded = unquote_plus(decoded)
        if next_decoded == decoded:
            break
        decoded = next_decoded
    return redacted


def redact_text(
    value: str,
    *,
    sensitive_values: Collection[str] = (),
) -> str:
    return _redact_runtime_string(
        value,
        _clean_sensitive_values(sensitive_values),
    )


def redact_headers(
    headers: Mapping[str, str],
    *,
    sensitive_values: Collection[str] = (),
) -> dict[str, str]:
    secrets = _clean_sensitive_values(sensitive_values)
    redacted: dict[str, str] = {}
    for key, value in headers.items():
        safe_key = _redact_string(str(key), secrets)
        redacted[safe_key] = (
            REDACTED
            if _is_sensitive_key(str(key))
            else _redact_runtime_string(str(value), secrets)
        )
    return redacted


def _redact_json_value(
    value: Any,
    *,
    sensitive_values: tuple[str, ...],
    depth: int,
    max_depth: int,
    redact_embedded_credentials: bool = False,
) -> object:
    if depth >= max_depth and isinstance(value, (dict, list)):
        return TRUNCATED
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_CONTAINER_ITEMS:
                result[TRUNCATED] = TRUNCATED
                break
            string_key = str(key)
            safe_key = _redact_string(string_key, sensitive_values)
            if _is_sensitive_key(string_key):
                result[safe_key] = REDACTED
            else:
                result[safe_key] = _redact_json_value(
                    item,
                    sensitive_values=sensitive_values,
                    depth=depth + 1,
                    max_depth=max_depth,
                    redact_embedded_credentials=redact_embedded_credentials,
                )
        return result
    if isinstance(value, list):
        items = value[:MAX_CONTAINER_ITEMS]
        result = [
            _redact_json_value(
                item,
                sensitive_values=sensitive_values,
                depth=depth + 1,
                max_depth=max_depth,
                redact_embedded_credentials=redact_embedded_credentials,
            )
            for item in items
        ]
        if len(value) > MAX_CONTAINER_ITEMS:
            result.append(TRUNCATED)
        return result
    if isinstance(value, str):
        if redact_embedded_credentials:
            return _redact_runtime_string(value, sensitive_values)
        return _redact_string(value, sensitive_values)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return TRUNCATED


def parse_redacted_json_body(
    body: bytes | None,
    content_type: str,
    *,
    sensitive_values: Collection[str] = (),
    max_depth: int = MAX_BODY_DEPTH,
) -> object | None:
    if body is None or len(body) > MAX_CAPTURE_BODY_BYTES:
        return None
    media_type = content_type.partition(";")[0].strip().lower()
    if media_type != "application/json" and not media_type.endswith("+json"):
        return None
    if max_depth < 1:
        raise ValueError("max_depth must be positive")
    try:
        parsed = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return _redact_json_value(
        parsed,
        sensitive_values=_clean_sensitive_values(sensitive_values),
        depth=0,
        max_depth=max_depth,
        redact_embedded_credentials=True,
    )


def response_body_may_be_captured(headers: Mapping[str, str]) -> bool:
    normalized_headers = {
        str(key).lower(): str(value)
        for key, value in headers.items()
    }
    raw_length = normalized_headers.get("content-length")
    if raw_length is None:
        return False
    try:
        length = int(raw_length)
    except ValueError:
        return False
    return 0 <= length <= MAX_CAPTURE_BODY_BYTES


def _trace_has_action_pair(payload: bytes) -> bool:
    before_call_ids: set[str] = set()
    after_call_ids: set[str] = set()
    for line in payload.splitlines():
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not isinstance(event, dict):
            continue
        call_id = event.get("callId")
        if not isinstance(call_id, str) or not call_id:
            continue
        if event.get("type") == "before":
            before_call_ids.add(call_id)
        elif event.get("type") == "after":
            after_call_ids.add(call_id)
    return bool(before_call_ids & after_call_ids)


def _reject_symbolic_link(path: Path) -> None:
    if path.is_symlink():
        raise TraceSanitizationError("trace sanitization failed")


def _open_exclusive_binary(path: Path):
    _reject_symbolic_link(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError:
        raise TraceSanitizationError(
            "trace sanitization failed"
        ) from None
    return os.fdopen(descriptor, "w+b")


def _trace_secret_variants(
    sensitive_values: Collection[str],
) -> tuple[bytes, ...]:
    variants: set[bytes] = set()
    for value in sensitive_values:
        if not isinstance(value, str) or not value:
            continue
        strings = set(_clean_sensitive_values((value,)))
        strings.update(
            {
                json.dumps(value, ensure_ascii=True)[1:-1],
                json.dumps(value, ensure_ascii=False)[1:-1],
            }
        )
        variants.update(
            item.encode("utf-8")
            for item in strings
            if item
        )
    return tuple(sorted(variants, key=len, reverse=True))


def _sanitize_trace_json_lines(
    payload: bytes,
    *,
    entry_name: str,
    sensitive_values: tuple[str, ...],
) -> bytes:
    if len(payload) > MAX_TRACE_METADATA_BYTES:
        return b""
    sanitized_lines: list[bytes] = []
    for line in payload.splitlines():
        try:
            parsed = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(parsed, dict):
            continue
        if (
            entry_name == "trace.trace"
            and parsed.get("type") not in _SAFE_TRACE_EVENT_TYPES
        ):
            continue
        sanitized = _redact_json_value(
            parsed,
            sensitive_values=sensitive_values,
            depth=0,
            max_depth=MAX_BODY_DEPTH,
        )
        sanitized_lines.append(
            json.dumps(
                sanitized,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
    if not sanitized_lines:
        return b""
    return b"\n".join(sanitized_lines) + b"\n"


def _assert_trace_has_no_secrets(
    trace_path: Path,
    *,
    sensitive_values: Collection[str],
) -> None:
    variants = _trace_secret_variants(sensitive_values)
    try:
        with ZipFile(trace_path) as trace_zip:
            entries = trace_zip.infolist()
            if not entries:
                raise TraceSanitizationError(
                    "trace sanitization failed"
                )
            for entry in entries:
                entry_payload = trace_zip.read(entry)
                payload = entry.filename.encode("utf-8") + entry_payload
                if any(variant in payload for variant in variants):
                    raise TraceSanitizationError(
                        "trace sanitization failed"
                    )
                if _BEARER_PATTERN.search(payload):
                    raise TraceSanitizationError(
                        "trace sanitization failed"
                    )
                decoded = payload.decode("utf-8", errors="ignore")
                for _ in range(3):
                    if _EMBEDDED_CREDENTIAL_PATTERN.search(decoded):
                        raise TraceSanitizationError(
                            "trace sanitization failed"
                        )
                    next_decoded = unquote_plus(decoded)
                    if next_decoded == decoded:
                        break
                    decoded = next_decoded
                for line in entry_payload.splitlines():
                    try:
                        parsed = json.loads(line)
                    except (
                        UnicodeDecodeError,
                        json.JSONDecodeError,
                    ):
                        raise TraceSanitizationError(
                            "trace sanitization failed"
                        ) from None
                    rescanned = _redact_json_value(
                        parsed,
                        sensitive_values=(),
                        depth=0,
                        max_depth=MAX_BODY_DEPTH,
                        redact_embedded_credentials=True,
                    )
                    if rescanned != parsed:
                        raise TraceSanitizationError(
                            "trace sanitization failed"
                        )
    except (BadZipFile, OSError):
        raise TraceSanitizationError(
            "trace sanitization failed"
        ) from None


def sanitize_trace_zip(
    raw_trace_path: Path,
    temporary_trace_path: Path,
    destination_trace_path: Path,
    *,
    sensitive_values: Collection[str],
) -> None:
    """Build a metadata-only trace because native traces retain secrets.

    DOM snapshots, screencasts, network snapshots, and resources are dropped.
    The sanitized action metadata is intentionally less complete than a native
    Playwright trace; credential safety takes priority for this local agent.
    """

    secrets = _clean_sensitive_values(sensitive_values)
    try:
        _reject_symbolic_link(raw_trace_path)
        _reject_symbolic_link(temporary_trace_path)
        _reject_symbolic_link(destination_trace_path)
        with ZipFile(raw_trace_path) as source_zip:
            sanitized_entries: dict[str, bytes] = {}
            for entry in source_zip.infolist():
                if entry.filename not in _SAFE_TRACE_ENTRIES:
                    continue
                if entry.file_size > MAX_TRACE_METADATA_BYTES:
                    continue
                payload = _sanitize_trace_json_lines(
                    source_zip.read(entry),
                    entry_name=entry.filename,
                    sensitive_values=secrets,
                )
                if payload:
                    sanitized_entries[entry.filename] = payload
        if "trace.trace" not in sanitized_entries:
            raise TraceSanitizationError("trace sanitization failed")
        if not _trace_has_action_pair(sanitized_entries["trace.trace"]):
            raise TraceSanitizationError("trace sanitization failed")

        with _open_exclusive_binary(temporary_trace_path) as trace_file:
            with ZipFile(
                trace_file,
                mode="w",
                compression=ZIP_DEFLATED,
            ) as destination_zip:
                for entry_name, payload in sanitized_entries.items():
                    destination_zip.writestr(entry_name, payload)

        _assert_trace_has_no_secrets(
            temporary_trace_path,
            sensitive_values=sensitive_values,
        )
        _reject_symbolic_link(destination_trace_path)
        os.replace(temporary_trace_path, destination_trace_path)
        destination_trace_path.chmod(0o600)
    except TraceSanitizationError:
        for unsafe_path in (
            temporary_trace_path,
            destination_trace_path,
        ):
            try:
                unsafe_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    except (BadZipFile, OSError, RuntimeError, ValueError):
        for unsafe_path in (
            temporary_trace_path,
            destination_trace_path,
        ):
            try:
                unsafe_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise TraceSanitizationError(
            "trace sanitization failed"
        ) from None
