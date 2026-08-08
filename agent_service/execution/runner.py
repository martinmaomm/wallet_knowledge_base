from __future__ import annotations

import asyncio
import os
import re
import stat
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from secrets import token_hex
from typing import Literal

from playwright.async_api import BrowserContext, Page, Request
from pydantic import BaseModel, ConfigDict, Field

from agent_service.execution.network import (
    NetworkEntry,
    is_same_origin_request,
    normalized_path,
    parse_redacted_json_body,
    redact_headers,
    redact_text,
    response_body_may_be_captured,
    sanitize_trace_zip,
    validate_allowed_origin,
)
from agent_service.schemas import TestCase, TestStep


MAX_CASE_ID_LENGTH = 80
DEFAULT_MAX_NETWORK_ENTRIES = 100
NETWORK_IDLE_TIMEOUT_SECONDS = 3.0
NETWORK_DRAIN_TIMEOUT_SECONDS = 3.0
NETWORK_START_GRACE_SECONDS = 0.1
_CASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_ARTIFACT_PART_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class StepExecutionError(RuntimeError):
    """A stable error boundary that never includes page or credential data."""


class EvidenceCaptureError(RuntimeError):
    """A stable error boundary for screenshot or trace failures."""


class ExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    status: Literal["completed", "failed"]
    error: str = ""
    screenshot_paths: tuple[str, ...] = Field(default_factory=tuple)
    trace_path: str
    network_inventory: tuple[NetworkEntry, ...] = Field(
        default_factory=tuple
    )


@dataclass(repr=False)
class RunnerContext:
    page: Page
    browser_context: BrowserContext
    artifacts_dir: Path
    allowed_origin: str
    recipient_account: str
    transaction_password: str
    payer_account: str = ""
    valid_transfer_amount: Decimal | None = None
    available_balance: Decimal | None = None
    max_network_entries: int = DEFAULT_MAX_NETWORK_ENTRIES

    def __repr__(self) -> str:
        return "RunnerContext([REDACTED])"

    @property
    def sensitive_values(self) -> frozenset[str]:
        return frozenset(
            value
            for value in (
                self.payer_account,
                self.recipient_account,
                self.transaction_password,
            )
            if value
        )


def validate_case_id(case_id: str) -> str:
    if (
        not isinstance(case_id, str)
        or not case_id
        or len(case_id) > MAX_CASE_ID_LENGTH
        or case_id in {".", ".."}
        or _CASE_ID_PATTERN.fullmatch(case_id) is None
    ):
        raise ValueError("case_id contains unsafe characters")
    return case_id


def safe_artifact_path(
    artifacts_dir: Path,
    case_id: str,
    artifact_part: str,
) -> Path:
    safe_case_id = validate_case_id(case_id)
    if (
        not artifact_part
        or len(artifact_part) > MAX_CASE_ID_LENGTH
        or artifact_part in {".", ".."}
        or _ARTIFACT_PART_PATTERN.fullmatch(artifact_part) is None
    ):
        raise ValueError("artifact name contains unsafe characters")

    if artifacts_dir.is_symlink():
        raise ValueError("artifacts directory must not be a symbolic link")
    artifacts_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    if artifacts_dir.is_symlink():
        raise ValueError("artifacts directory must not be a symbolic link")
    root = artifacts_dir.resolve(strict=True)
    root.chmod(0o700)
    candidate = root / f"{safe_case_id}-{artifact_part}"
    if candidate.is_symlink():
        raise ValueError("artifact path must not be a symbolic link")
    target = candidate.resolve(strict=False)
    if target.parent != root:
        raise ValueError("artifact path escapes artifacts directory")
    return target


def _required_decimal(
    value: Decimal | None,
    *,
    allow_zero: bool,
) -> Decimal:
    if value is None:
        raise StepExecutionError(
            "required runner context is unavailable"
        )
    try:
        decimal_value = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        raise StepExecutionError(
            "required runner context is unavailable"
        ) from None
    if not decimal_value.is_finite():
        raise StepExecutionError(
            "required runner context is unavailable"
        )
    if decimal_value < 0 or (not allow_zero and decimal_value == 0):
        raise StepExecutionError(
            "required runner context is unavailable"
        )
    return decimal_value


def _render_decimal(value: Decimal) -> str:
    rendered = format(value, "f")
    if not rendered:
        raise StepExecutionError(
            "required runner context is unavailable"
        )
    return rendered


def _resolve_amount(step: TestStep, context: RunnerContext) -> str:
    if step.value is not None:
        return step.value
    if step.source == "valid_transfer_amount":
        amount = _required_decimal(
            context.valid_transfer_amount,
            allow_zero=False,
        )
        if context.available_balance is not None:
            balance = _required_decimal(
                context.available_balance,
                allow_zero=True,
            )
            if amount > balance:
                raise StepExecutionError(
                    "required runner context is unavailable"
                )
        return _render_decimal(amount)
    if step.source == "amount_above_available_balance":
        balance = _required_decimal(
            context.available_balance,
            allow_zero=True,
        )
        above_balance = balance + Decimal("1")
        if above_balance <= balance:
            above_balance = balance.next_plus()
        if not above_balance.is_finite() or above_balance <= balance:
            raise StepExecutionError(
                "required runner context is unavailable"
            )
        return _render_decimal(above_balance)
    raise StepExecutionError("required runner context is unavailable")


async def _login(
    step: TestStep,
    context: RunnerContext,
) -> None:
    raise StepExecutionError("step execution failed")


async def _open_internal_transfer(
    step: TestStep,
    context: RunnerContext,
) -> None:
    await context.page.get_by_test_id("internal-transfer").click()


async def _select_asset(
    step: TestStep,
    context: RunnerContext,
) -> None:
    await context.page.get_by_test_id("asset-select").select_option(
        step.value
    )


async def _fill_recipient(
    step: TestStep,
    context: RunnerContext,
) -> None:
    value = step.value
    if step.source == "recipient_account":
        value = context.recipient_account
        if not value:
            raise StepExecutionError(
                "required runner context is unavailable"
            )
    if value is None:
        raise StepExecutionError(
            "required runner context is unavailable"
        )
    await context.page.get_by_test_id("recipient").fill(value)


async def _fill_amount(
    step: TestStep,
    context: RunnerContext,
) -> None:
    await context.page.get_by_test_id("amount").fill(
        _resolve_amount(step, context)
    )


async def _submit(
    step: TestStep,
    context: RunnerContext,
) -> None:
    await context.page.get_by_test_id("submit-transfer").click()


async def _complete_security_verification(
    step: TestStep,
    context: RunnerContext,
) -> None:
    if not context.transaction_password:
        raise StepExecutionError(
            "required runner context is unavailable"
        )
    await context.page.get_by_test_id("transaction-password").fill(
        context.transaction_password
    )
    await context.page.get_by_test_id("confirm-security").click()


async def _refresh_transaction_history(
    step: TestStep,
    context: RunnerContext,
) -> None:
    await context.page.get_by_test_id(
        "transaction-history-refresh"
    ).click()


ActionHandler = Callable[
    [TestStep, RunnerContext],
    Awaitable[None],
]

_ACTION_REGISTRY: dict[str, ActionHandler] = {
    "login": _login,
    "open_internal_transfer": _open_internal_transfer,
    "select_asset": _select_asset,
    "fill_recipient": _fill_recipient,
    "fill_amount": _fill_amount,
    "submit": _submit,
    "complete_security_verification": _complete_security_verification,
    "refresh_transaction_history": _refresh_transaction_history,
}


async def execute_step(
    step: TestStep,
    context: RunnerContext,
) -> None:
    try:
        validated = TestStep.model_validate(step.model_dump())
        handler = _ACTION_REGISTRY.get(validated.action)
        if handler is None:
            raise StepExecutionError("step execution failed")
        await handler(validated, context)
    except StepExecutionError:
        raise
    except Exception:
        raise StepExecutionError("step execution failed") from None


class _NetworkCollector:
    def __init__(
        self,
        *,
        allowed_origin: str,
        sensitive_values: Collection[str],
        max_entries: int,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_network_entries must be positive")
        self.allowed_origin = allowed_origin
        self.sensitive_values = tuple(sensitive_values)
        self.max_entries = max_entries
        self.entries: list[NetworkEntry] = []
        self.tasks: set[asyncio.Task[None]] = set()
        self._scheduled = 0
        self._inflight: dict[int, Request] = {}
        self._completed_request_ids: set[int] = set()
        self._idle = asyncio.Event()
        self._idle.set()

    def request_started(self, request: Request) -> None:
        if not is_same_origin_request(
            request.url,
            self.allowed_origin,
        ):
            return
        request_id = id(request)
        if request_id in self._completed_request_ids:
            return
        self._inflight[request_id] = request
        self._idle.clear()

    def request_finished(self, request: Request) -> None:
        self._complete_request(request)

    def request_failed(self, request: Request) -> None:
        self._complete_request(request)

    def schedule(self, request: Request) -> None:
        self._complete_request(request)

    def _complete_request(self, request: Request) -> None:
        if not is_same_origin_request(
            request.url,
            self.allowed_origin,
        ):
            return
        request_id = id(request)
        if request_id in self._completed_request_ids:
            return
        self._completed_request_ids.add(request_id)
        self._inflight.pop(request_id, None)
        if not self._inflight:
            self._idle.set()
        if self._scheduled >= self.max_entries:
            return
        self._scheduled += 1
        task = asyncio.create_task(self._capture(request))
        self.tasks.add(task)

    async def _capture(self, request: Request) -> None:
        try:
            response = await request.response()
            request_headers = redact_headers(
                request.headers,
                sensitive_values=self.sensitive_values,
            )
            request_body = parse_redacted_json_body(
                request.post_data_buffer,
                request.headers.get("content-type", ""),
                sensitive_values=self.sensitive_values,
            )
            response_headers: dict[str, str] = {}
            response_body = None
            status = None
            if response is not None:
                status = response.status
                raw_response_headers = await response.all_headers()
                response_headers = redact_headers(
                    raw_response_headers,
                    sensitive_values=self.sensitive_values,
                )
                if response_body_may_be_captured(
                    raw_response_headers
                ):
                    response_body = parse_redacted_json_body(
                        await response.body(),
                        raw_response_headers.get("content-type", ""),
                        sensitive_values=self.sensitive_values,
                    )
            safe_path = redact_text(
                normalized_path(request.url),
                sensitive_values=self.sensitive_values,
            )
            self.entries.append(
                NetworkEntry(
                    method=request.method,
                    path=safe_path,
                    status=status,
                    request_headers=request_headers,
                    response_headers=response_headers,
                    request_body=request_body,
                    response_body=response_body,
                )
            )
        except Exception:
            return

    async def wait_for_idle(self, timeout: float) -> None:
        await asyncio.sleep(NETWORK_START_GRACE_SECONDS)
        if not self._inflight:
            return
        await asyncio.wait_for(self._idle.wait(), timeout=timeout)

    def stop_tracking(self) -> None:
        self._inflight.clear()
        self._idle.set()

    async def drain(self) -> None:
        if not self.tasks:
            return
        done, pending = await asyncio.wait(
            tuple(self.tasks),
            timeout=NETWORK_DRAIN_TIMEOUT_SECONDS,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        self.tasks.clear()


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, EvidenceCaptureError):
        return "EvidenceCaptureError: evidence capture failed"
    return f"{type(exc).__name__}: step execution failed"


def _remove_artifact(path: Path) -> bool:
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        pass

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        return False

    flags = os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        os.close(descriptor)
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def _assert_safe_write_target(path: Path) -> None:
    if path.is_symlink():
        raise EvidenceCaptureError("evidence capture failed")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise EvidenceCaptureError(
            "evidence capture failed"
        ) from None
    if not stat.S_ISREG(metadata.st_mode):
        raise EvidenceCaptureError("evidence capture failed")


def _assert_private_regular_file(path: Path) -> None:
    if path.is_symlink():
        raise EvidenceCaptureError("evidence capture failed")
    try:
        metadata = path.lstat()
    except OSError:
        raise EvidenceCaptureError(
            "evidence capture failed"
        ) from None
    if not stat.S_ISREG(metadata.st_mode):
        raise EvidenceCaptureError("evidence capture failed")
    path.chmod(0o600)


def _replace_artifact(source: Path, destination: Path) -> None:
    _assert_private_regular_file(source)
    _assert_safe_write_target(destination)
    try:
        os.replace(source, destination)
    except OSError:
        raise EvidenceCaptureError(
            "evidence capture failed"
        ) from None
    _assert_private_regular_file(destination)


def _case_sensitive_values(
    case: TestCase,
    context: RunnerContext,
) -> frozenset[str]:
    fixed_recipients = {
        step.value
        for step in case.steps
        if step.action == "fill_recipient" and step.value
    }
    return frozenset(
        {*context.sensitive_values, *fixed_recipients}
    )


@dataclass(frozen=True)
class _CleanupResult:
    failed: bool
    screenshot_paths: tuple[str, ...]
    trace_path: str


def _consume_pending_cancellation() -> None:
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        task.uncancel()


async def _cleanup_case(
    *,
    context: RunnerContext,
    collector: _NetworkCollector,
    listeners: tuple[tuple[str, Callable[[Request], None]], ...],
    tracing_attempted: bool,
    screenshot_path: Path,
    screenshot_temporary_path: Path,
    raw_trace_path: Path,
    temporary_trace_path: Path,
    trace_path: Path,
    sensitive_values: Collection[str],
) -> _CleanupResult:
    cleanup_failed = False
    screenshot_paths: list[str] = []
    result_trace_path = ""

    for event_name, listener in listeners:
        try:
            context.page.remove_listener(event_name, listener)
        except Exception:
            cleanup_failed = True
    collector.stop_tracking()
    try:
        await collector.drain()
    except Exception:
        cleanup_failed = True

    try:
        _assert_safe_write_target(screenshot_temporary_path)
        mask = [
            context.page.get_by_test_id("recipient"),
            context.page.get_by_test_id("transaction-password"),
            context.page.get_by_test_id("payer-account"),
        ]
        get_by_text = getattr(context.page, "get_by_text", None)
        if callable(get_by_text):
            mask.extend(
                get_by_text(secret, exact=False)
                for secret in sorted(sensitive_values)
                if secret
            )
        await context.page.screenshot(
            path=str(screenshot_temporary_path),
            full_page=True,
            mask=mask,
            mask_color="#000000",
        )
        _replace_artifact(
            screenshot_temporary_path,
            screenshot_path,
        )
        screenshot_paths.append(str(screenshot_path))
    except Exception:
        _remove_artifact(screenshot_temporary_path)
        _remove_artifact(screenshot_path)
        cleanup_failed = True

    if tracing_attempted:
        try:
            _assert_safe_write_target(raw_trace_path)
            await context.browser_context.tracing.stop(
                path=str(raw_trace_path)
            )
            _assert_private_regular_file(raw_trace_path)
            sanitize_trace_zip(
                raw_trace_path,
                temporary_trace_path,
                trace_path,
                sensitive_values=sensitive_values,
            )
            _assert_private_regular_file(trace_path)
            result_trace_path = str(trace_path)
        except asyncio.CancelledError:
            _remove_artifact(trace_path)
            cleanup_failed = True
        except Exception:
            _remove_artifact(trace_path)
            cleanup_failed = True

    for temporary_path in (
        screenshot_temporary_path,
        raw_trace_path,
        temporary_trace_path,
    ):
        if not _remove_artifact(temporary_path):
            cleanup_failed = True

    return _CleanupResult(
        failed=cleanup_failed,
        screenshot_paths=tuple(screenshot_paths),
        trace_path=result_trace_path,
    )


async def _await_cleanup_reliably(
    cleanup_task: asyncio.Task[_CleanupResult],
) -> tuple[_CleanupResult, asyncio.CancelledError | None]:
    cancellation_error: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(cleanup_task)
            return result, cancellation_error
        except asyncio.CancelledError as exc:
            current_task = asyncio.current_task()
            outer_cancelled = (
                current_task is not None
                and current_task.cancelling() > 0
            )
            if outer_cancelled:
                if cancellation_error is None:
                    cancellation_error = exc
                _consume_pending_cancellation()
            if cleanup_task.done() and cleanup_task.cancelled():
                return (
                    _CleanupResult(
                        failed=True,
                        screenshot_paths=(),
                        trace_path="",
                    ),
                    cancellation_error,
                )
        except Exception:
            return (
                _CleanupResult(
                    failed=True,
                    screenshot_paths=(),
                    trace_path="",
                ),
                cancellation_error,
            )


async def run_case(
    case: TestCase,
    context: RunnerContext,
) -> ExecutionResult:
    validated_origin = validate_allowed_origin(context.allowed_origin)
    validated_case = TestCase.model_validate(case.model_dump())
    validate_case_id(validated_case.case_id)
    trace_path = safe_artifact_path(
        context.artifacts_dir,
        validated_case.case_id,
        "trace.zip",
    )
    screenshot_path = safe_artifact_path(
        context.artifacts_dir,
        validated_case.case_id,
        "final.png",
    )
    nonce = token_hex(8)
    raw_trace_path = safe_artifact_path(
        context.artifacts_dir,
        validated_case.case_id,
        f"raw-trace-{nonce}.zip",
    )
    temporary_trace_path = safe_artifact_path(
        context.artifacts_dir,
        validated_case.case_id,
        f"sanitized-trace-{nonce}.tmp",
    )
    screenshot_temporary_path = safe_artifact_path(
        context.artifacts_dir,
        validated_case.case_id,
        f"masked-screenshot-{nonce}.tmp.png",
    )
    trace_removed = _remove_artifact(trace_path)
    screenshot_removed = _remove_artifact(screenshot_path)
    if not trace_removed or not screenshot_removed:
        raise EvidenceCaptureError("evidence capture failed")
    sensitive_values = _case_sensitive_values(
        validated_case,
        context,
    )
    collector = _NetworkCollector(
        allowed_origin=validated_origin,
        sensitive_values=sensitive_values,
        max_entries=context.max_network_entries,
    )

    execution_error: Exception | None = None
    cancellation_error: asyncio.CancelledError | None = None
    fatal_error: BaseException | None = None
    tracing_attempted = False
    listeners: list[tuple[str, Callable[[Request], None]]] = []
    try:
        tracing_attempted = True
        await context.browser_context.tracing.start(
            screenshots=True,
            snapshots=True,
        )
        for event_name, listener in (
            ("request", collector.request_started),
            ("requestfinished", collector.request_finished),
            ("requestfailed", collector.request_failed),
        ):
            context.page.on(event_name, listener)
            listeners.append((event_name, listener))

        for step in validated_case.steps:
            await execute_step(step, context)
        try:
            await collector.wait_for_idle(
                NETWORK_IDLE_TIMEOUT_SECONDS
            )
        except TimeoutError:
            raise EvidenceCaptureError(
                "evidence capture failed"
            ) from None
    except asyncio.CancelledError as exc:
        cancellation_error = exc
        _consume_pending_cancellation()
    except Exception as exc:
        execution_error = exc
    except BaseException as exc:
        fatal_error = exc

    cleanup_task = asyncio.create_task(
        _cleanup_case(
            context=context,
            collector=collector,
            listeners=tuple(listeners),
            tracing_attempted=tracing_attempted,
            screenshot_path=screenshot_path,
            screenshot_temporary_path=screenshot_temporary_path,
            raw_trace_path=raw_trace_path,
            temporary_trace_path=temporary_trace_path,
            trace_path=trace_path,
            sensitive_values=sensitive_values,
        )
    )
    cleanup_result, cleanup_cancellation = await _await_cleanup_reliably(
        cleanup_task
    )
    if cancellation_error is None:
        cancellation_error = cleanup_cancellation
    if cleanup_result.failed and execution_error is None:
        execution_error = EvidenceCaptureError(
            "evidence capture failed"
        )

    if cancellation_error is not None:
        raise cancellation_error
    if fatal_error is not None:
        raise fatal_error

    return ExecutionResult(
        case_id=validated_case.case_id,
        status="failed" if execution_error is not None else "completed",
        error=_safe_error(execution_error) if execution_error else "",
        screenshot_paths=cleanup_result.screenshot_paths,
        trace_path=cleanup_result.trace_path,
        network_inventory=tuple(collector.entries),
    )
