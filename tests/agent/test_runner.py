import asyncio
import json
import struct
import zlib
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote, quote_plus
from zipfile import ZipFile

import pytest
from pydantic import ValidationError
from playwright.async_api import async_playwright

import agent_service.execution.runner as runner_module
from agent_service.execution.network import (
    MAX_CAPTURE_BODY_BYTES,
    NetworkEntry,
    TraceSanitizationError,
    is_same_origin_request,
    normalized_path,
    parse_redacted_json_body,
    redact_headers,
    redact_text,
    response_body_may_be_captured,
    sanitize_trace_zip,
    validate_allowed_origin,
)
from agent_service.execution.runner import (
    RunnerContext,
    StepExecutionError,
    _NetworkCollector,
    execute_step,
    run_case,
    safe_artifact_path,
    validate_case_id,
)
from agent_service.schemas import TestCase as DslTestCase
from agent_service.schemas import TestStep as DslTestStep


def test_network_entry_is_strict_and_frozen() -> None:
    entry = NetworkEntry(method="POST", path="/api/transfer", status=200)

    with pytest.raises(ValidationError):
        entry.status = 201

    with pytest.raises(ValidationError, match="extra_forbidden"):
        NetworkEntry(
            method="POST",
            path="/api/transfer",
            status=200,
            secret="must not be stored",
        )

    with pytest.raises(ValidationError):
        NetworkEntry(
            method="POST",
            path="/api/transfer",
            status="200",
        )


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
def test_allowed_origin_requires_strict_http_pure_origin(origin: str) -> None:
    with pytest.raises(ValueError, match="allowed_origin"):
        validate_allowed_origin(origin)


def test_network_matching_is_exact_origin_and_discards_query_fragment() -> None:
    origin = validate_allowed_origin("https://wallet-test.local/")

    assert origin == "https://wallet-test.local"
    assert is_same_origin_request(
        "https://wallet-test.local/api/transfer?token=secret#ignored",
        origin,
    )
    assert normalized_path(
        "https://wallet-test.local/api/transfer?token=secret#ignored"
    ) == "/api/transfer"
    assert not is_same_origin_request(
        "https://wallet-test.local.evil/api/transfer",
        origin,
    )
    assert not is_same_origin_request(
        "https://user@wallet-test.local/api/transfer",
        origin,
    )
    assert not is_same_origin_request(
        "file:///tmp/transfer",
        origin,
    )


def test_network_headers_are_redacted_case_insensitively() -> None:
    headers = redact_headers(
        {
            "Authorization": "Bearer secret-token",
            "COOKIE": "session=secret-cookie",
            "Set-Cookie": "session=response-cookie",
            "x-API-key": "secret-api-key",
            "X-Recipient-Account": "recipient@example.test",
            "Content-Type": "application/json",
        },
        sensitive_values={"recipient@example.test"},
    )

    assert headers == {
        "Authorization": "[REDACTED]",
        "COOKIE": "[REDACTED]",
        "Set-Cookie": "[REDACTED]",
        "x-API-key": "[REDACTED]",
        "X-Recipient-Account": "[REDACTED]",
        "Content-Type": "application/json",
    }
    assert "secret" not in repr(headers)
    assert "recipient@example.test" not in repr(headers)


def test_json_body_is_recursively_redacted_and_depth_limited() -> None:
    body = (
        b'{"recipient_account":"recipient@example.test",'
        b'"profile":{"display":"recipient@example.test","safe":"ok"},'
        b'"nested":{"one":{"two":{"three":{"value":"too-deep"}}}},'
        b'"token":"secret-token"}'
    )

    redacted = parse_redacted_json_body(
        body,
        "application/json; charset=utf-8",
        sensitive_values={"recipient@example.test"},
        max_depth=3,
    )

    assert redacted["recipient_account"] == "[REDACTED]"
    assert redacted["profile"]["display"] == "[REDACTED]"
    assert redacted["profile"]["safe"] == "ok"
    assert redacted["token"] == "[REDACTED]"
    assert redacted["nested"]["one"]["two"] == "[TRUNCATED]"
    assert "recipient@example.test" not in repr(redacted)
    assert "secret-token" not in repr(redacted)
    assert "too-deep" not in repr(redacted)


def test_json_body_redacts_unknown_token_patterns_in_generic_fields() -> None:
    redacted = parse_redacted_json_body(
        (
            b'{"message":"Bearer unknown-inventory-token",'
            b'"detail":"token=second-unknown-token",'
            b'"api_note":"api-key: third-unknown-token"}'
        ),
        "application/json",
    )

    assert redacted == {
        "message": "[REDACTED]",
        "detail": "[REDACTED]",
        "api_note": "[REDACTED]",
    }
    assert "unknown-token" not in repr(redacted)


def test_json_body_redacts_nested_json_string_credentials() -> None:
    redacted = parse_redacted_json_body(
        b'{"message":"{\\"token\\":\\"nested-token-value\\"}"}',
        "application/json",
    )

    assert redacted == {"message": "[REDACTED]"}
    assert "nested-token-value" not in repr(redacted)


def test_body_capture_rejects_oversized_or_non_json_content() -> None:
    assert (
        parse_redacted_json_body(
            b"x" * (MAX_CAPTURE_BODY_BYTES + 1),
            "application/json",
        )
        is None
    )
    assert (
        parse_redacted_json_body(
            b'{"password":"must-not-be-read"}',
            "text/plain",
        )
        is None
    )


def test_redact_text_removes_percent_encoded_account_values() -> None:
    redacted = redact_text(
        "/api/account/recipient%40example.test",
        sensitive_values={"recipient@example.test"},
    )

    assert redacted == "/api/account/[REDACTED]"
    assert "recipient" not in redacted


def test_network_collector_skips_known_oversized_response_body() -> None:
    class OversizedResponse:
        status = 200
        body_calls = 0

        async def all_headers(self) -> dict[str, str]:
            return {
                "content-type": "application/json",
                "content-length": str(MAX_CAPTURE_BODY_BYTES + 1),
            }

        async def body(self) -> bytes:
            self.body_calls += 1
            raise AssertionError("oversized body must not be loaded")

    class FakeRequest:
        url = "https://wallet-test.local/api/large"
        method = "GET"
        headers: dict[str, str] = {}
        post_data_buffer = None

        def __init__(self, response: OversizedResponse) -> None:
            self._response = response

        async def response(self) -> OversizedResponse:
            return self._response

    async def exercise() -> tuple[OversizedResponse, _NetworkCollector]:
        response = OversizedResponse()
        collector = _NetworkCollector(
            allowed_origin="https://wallet-test.local",
            sensitive_values=(),
            max_entries=1,
        )
        await collector._capture(FakeRequest(response))
        return response, collector

    response, collector = asyncio.run(exercise())

    assert response.body_calls == 0
    assert len(collector.entries) == 1
    assert collector.entries[0].response_body is None


@pytest.mark.parametrize(
    "headers",
    [
        {"content-type": "application/json"},
        {
            "content-type": "application/json",
            "content-length": "invalid",
        },
        {
            "content-type": "application/json",
            "content-length": "-1",
        },
    ],
)
def test_network_collector_never_reads_body_without_safe_content_length(
    headers: dict[str, str],
) -> None:
    class UnknownLengthResponse:
        status = 200
        body_calls = 0

        async def all_headers(self) -> dict[str, str]:
            return headers

        async def body(self) -> bytes:
            self.body_calls += 1
            raise AssertionError("unbounded body must not be loaded")

    class FakeRequest:
        url = "https://wallet-test.local/api/unbounded"
        method = "GET"
        headers: dict[str, str] = {}
        post_data_buffer = None

        def __init__(self, response: UnknownLengthResponse) -> None:
            self._response = response

        async def response(self) -> UnknownLengthResponse:
            return self._response

    async def exercise() -> tuple[UnknownLengthResponse, _NetworkCollector]:
        response = UnknownLengthResponse()
        collector = _NetworkCollector(
            allowed_origin="https://wallet-test.local",
            sensitive_values=(),
            max_entries=1,
        )
        await collector._capture(FakeRequest(response))
        return response, collector

    response, collector = asyncio.run(exercise())

    assert not response_body_may_be_captured(headers)
    assert response.body_calls == 0
    assert len(collector.entries) == 1
    assert collector.entries[0].response_body is None


@pytest.mark.parametrize(
    "message",
    [
        "Bearer UNKNOWN-TRACE-TOKEN",
        "Bearer%20UNKNOWN-TRACE-TOKEN",
        "Bearer+UNKNOWN-TRACE-TOKEN",
        "token=UNKNOWN-TRACE-TOKEN",
        "token%3DUNKNOWN-TRACE-TOKEN",
        "api-key:+UNKNOWN-TRACE-TOKEN",
        '{"token":"NESTED-TRACE-TOKEN"}',
    ],
)
def test_trace_sanitizer_rejects_unknown_bearer_and_deletes_output(
    tmp_path: Path,
    message: str,
) -> None:
    raw_trace = tmp_path / "raw.zip"
    temporary_trace = tmp_path / "temporary.zip"
    destination_trace = tmp_path / "trace.zip"
    destination_trace.write_bytes(b"stale-trace-secret")
    with ZipFile(raw_trace, mode="w") as trace_zip:
        trace_zip.writestr(
            "trace.trace",
            json.dumps(
                {
                    "type": "context-options",
                    "message": message,
                }
            )
            + "\n",
        )

    with pytest.raises(
        TraceSanitizationError,
        match="trace sanitization failed",
    ):
        sanitize_trace_zip(
            raw_trace,
            temporary_trace,
            destination_trace,
            sensitive_values=(),
        )

    assert not temporary_trace.exists()
    assert not destination_trace.exists()


@pytest.mark.parametrize(
    "events",
    [
        [{"type": "context-options", "browserName": "chromium"}],
        [
            {"type": "before", "callId": "call@1", "method": "click"},
            {"type": "after", "callId": "call@2"},
        ],
    ],
)
def test_trace_sanitizer_requires_matching_before_after_action_evidence(
    tmp_path: Path,
    events: list[dict[str, str]],
) -> None:
    raw_trace = tmp_path / "raw.zip"
    temporary_trace = tmp_path / "temporary.zip"
    destination_trace = tmp_path / "trace.zip"
    with ZipFile(raw_trace, mode="w") as trace_zip:
        trace_zip.writestr(
            "trace.trace",
            "".join(json.dumps(event) + "\n" for event in events),
        )

    with pytest.raises(
        TraceSanitizationError,
        match="trace sanitization failed",
    ):
        sanitize_trace_zip(
            raw_trace,
            temporary_trace,
            destination_trace,
            sensitive_values=(),
        )

    assert not temporary_trace.exists()
    assert not destination_trace.exists()


def test_trace_sanitizer_rejects_destination_symlink_replacement(
    tmp_path: Path,
) -> None:
    raw_trace = tmp_path / "raw.zip"
    temporary_trace = tmp_path / "temporary.zip"
    destination_trace = tmp_path / "trace.zip"
    outside = tmp_path / "outside.zip"
    outside.write_bytes(b"outside-must-not-change")
    destination_trace.symlink_to(outside)
    with ZipFile(raw_trace, mode="w") as trace_zip:
        trace_zip.writestr(
            "trace.trace",
            (
                json.dumps(
                    {
                        "type": "before",
                        "callId": "call@1",
                        "method": "click",
                    }
                )
                + "\n"
                + json.dumps({"type": "after", "callId": "call@1"})
                + "\n"
            ),
        )

    with pytest.raises(
        TraceSanitizationError,
        match="trace sanitization failed",
    ):
        sanitize_trace_zip(
            raw_trace,
            temporary_trace,
            destination_trace,
            sensitive_values=(),
        )

    assert outside.read_bytes() == b"outside-must-not-change"
    assert not destination_trace.exists()
    assert not temporary_trace.exists()


def _make_case(
    case_id: str,
    steps: list[DslTestStep | dict[str, str]],
) -> DslTestCase:
    return DslTestCase(
        case_id=case_id,
        title="Runner fixture",
        priority="P0",
        source_refs=[f"人工基准:{case_id}"],
        inferred=False,
        rationale="",
        preconditions=[],
        steps=steps,
        assertions=[{"type": "transfer_request_succeeded"}],
    )


def _secret_byte_variants(value: str) -> set[bytes]:
    variants = {
        value,
        quote(value, safe=""),
        quote_plus(value, safe=""),
        json.dumps(value, ensure_ascii=True)[1:-1],
        json.dumps(value, ensure_ascii=False)[1:-1],
    }
    return {
        variant.encode("utf-8")
        for variant in variants
        if variant
    }


def _png_pixel(path: Path, x: int, y: int) -> tuple[int, int, int]:
    data = path.read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    offset = 8
    width = height = color_type = 0
    compressed = bytearray()
    while offset < len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        chunk_data = data[offset + 8 : offset + 8 + length]
        offset += 12 + length
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(
                ">IIBB",
                chunk_data[:10],
            )
            assert bit_depth == 8
            assert color_type in {2, 6}
        elif chunk_type == b"IDAT":
            compressed.extend(chunk_data)
        elif chunk_type == b"IEND":
            break

    bytes_per_pixel = 3 if color_type == 2 else 4
    stride = width * bytes_per_pixel
    raw = zlib.decompress(bytes(compressed))
    rows: list[bytearray] = []

    def paeth(left: int, above: int, upper_left: int) -> int:
        estimate = left + above - upper_left
        left_distance = abs(estimate - left)
        above_distance = abs(estimate - above)
        upper_left_distance = abs(estimate - upper_left)
        if left_distance <= above_distance and left_distance <= upper_left_distance:
            return left
        if above_distance <= upper_left_distance:
            return above
        return upper_left

    raw_offset = 0
    for row_index in range(height):
        filter_type = raw[raw_offset]
        raw_offset += 1
        row = bytearray(raw[raw_offset : raw_offset + stride])
        raw_offset += stride
        previous = rows[row_index - 1] if row_index else bytearray(stride)
        for index in range(stride):
            left = row[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            above = previous[index]
            upper_left = (
                previous[index - bytes_per_pixel]
                if index >= bytes_per_pixel
                else 0
            )
            if filter_type == 1:
                row[index] = (row[index] + left) & 0xFF
            elif filter_type == 2:
                row[index] = (row[index] + above) & 0xFF
            elif filter_type == 3:
                row[index] = (row[index] + ((left + above) // 2)) & 0xFF
            elif filter_type == 4:
                row[index] = (
                    row[index] + paeth(left, above, upper_left)
                ) & 0xFF
            else:
                assert filter_type == 0
        rows.append(row)

    assert 0 <= x < width
    assert 0 <= y < height
    start = x * bytes_per_pixel
    return tuple(rows[y][start : start + 3])


def test_runner_executes_registered_actions_and_collects_redacted_api(
    tmp_path: Path,
) -> None:
    async def exercise():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                browser_context = await browser.new_context()
                page = await browser_context.new_page()
                page.set_default_timeout(500)

                async def same_origin_route(route) -> None:
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        headers={
                            "Set-Cookie": "session=response-cookie",
                            "X-Safe": "response-ok",
                        },
                        body=(
                            '{"recipient_account":"recipient@example.test",'
                            '"token":"response-token","status":"success"}'
                        ),
                    )

                async def outside_route(route) -> None:
                    await route.fulfill(
                        status=204,
                        body="outside-secret",
                    )

                await page.route(
                    "https://wallet-test.local/**",
                    same_origin_route,
                )
                await page.route(
                    "https://outside.invalid/**",
                    outside_route,
                )
                await page.set_content(
                    """
                    <style>
                      body { margin: 0; background: white; }
                      input {
                        display: block;
                        box-sizing: border-box;
                        width: 240px;
                        height: 40px;
                        margin: 0;
                        background: white;
                      }
                    </style>
                    <input data-testid="recipient" />
                    <input data-testid="amount" />
                    <input data-testid="transaction-password" />
                    <div data-testid="receipt-recipient">
                      recipient@example.test
                    </div>
                    <div data-testid="receipt-password">
                      transaction-secret
                    </div>
                    <div data-testid="receipt-payer">
                      payer@example.test
                    </div>
                    <button data-testid="confirm-security">Confirm</button>
                    <button data-testid="submit-transfer"
                      onclick="
                        const recipient =
                          document.querySelector('[data-testid=recipient]').value;
                        const password =
                          document.querySelector(
                            '[data-testid=transaction-password]'
                          ).value;
                        fetch(
                          'https://wallet-test.local/api/internal-transfer'
                            + '?recipient=' + encodeURIComponent(recipient),
                          {
                            method: 'POST',
                            headers: {
                              'Authorization': 'Bearer request-token',
                              'X-API-Key': 'request-api-key',
                              'X-Recipient-Account': recipient,
                              'Content-Type': 'application/json'
                            },
                            body: JSON.stringify({
                              recipient_account: recipient,
                              transaction_password: password,
                              nested: {token: 'nested-token'},
                              amount:
                                document.querySelector(
                                  '[data-testid=amount]'
                                ).value
                            })
                          }
                        );
                        fetch('https://outside.invalid/telemetry?token=outside');
                      ">
                      Submit
                    </button>
                    """
                )
                recipient_box = (
                    await page.get_by_test_id("recipient").bounding_box()
                )
                password_box = (
                    await page.get_by_test_id(
                        "transaction-password"
                    ).bounding_box()
                )
                assert recipient_box is not None
                assert password_box is not None
                repeated_secret_boxes = []
                for test_id in (
                    "receipt-recipient",
                    "receipt-password",
                    "receipt-payer",
                ):
                    box = await page.get_by_test_id(test_id).bounding_box()
                    assert box is not None
                    repeated_secret_boxes.append(box)
                case = _make_case(
                    "TC-OTI-002",
                    [
                        {
                            "action": "fill_recipient",
                            "source": "recipient_account",
                        },
                        {"action": "fill_amount", "value": "10"},
                        {"action": "complete_security_verification"},
                        {"action": "submit"},
                    ],
                )
                result = await run_case(
                    case,
                    RunnerContext(
                        page=page,
                        browser_context=browser_context,
                        artifacts_dir=tmp_path,
                        allowed_origin="https://wallet-test.local",
                        payer_account="payer@example.test",
                        recipient_account="recipient@example.test",
                        transaction_password="transaction-secret",
                    ),
                )
                return (
                    result,
                    recipient_box,
                    password_box,
                    repeated_secret_boxes,
                )
            finally:
                await browser.close()

    result, recipient_box, password_box, repeated_secret_boxes = asyncio.run(
        exercise()
    )

    assert result.status == "completed"
    assert result.error == ""
    assert Path(result.trace_path).is_file()
    assert len(result.screenshot_paths) == 1
    assert Path(result.screenshot_paths[0]).is_file()
    assert len(result.network_inventory) == 1
    entry = result.network_inventory[0]
    assert entry.method == "POST"
    assert entry.path == "/api/internal-transfer"
    assert entry.status == 200
    assert entry.request_body["amount"] == "10"
    assert entry.request_body["recipient_account"] == "[REDACTED]"
    assert entry.request_body["transaction_password"] == "[REDACTED]"
    assert entry.request_body["nested"]["token"] == "[REDACTED]"
    assert entry.response_body["recipient_account"] == "[REDACTED]"
    assert entry.response_body["token"] == "[REDACTED]"
    assert entry.request_headers["authorization"] == "[REDACTED]"
    assert entry.request_headers["x-api-key"] == "[REDACTED]"
    assert entry.request_headers["x-recipient-account"] == "[REDACTED]"
    assert entry.response_headers["x-safe"] == "response-ok"
    if "set-cookie" in entry.response_headers:
        assert entry.response_headers["set-cookie"] == "[REDACTED]"
    serialized = repr(result)
    for secret in (
        "payer@example.test",
        "recipient@example.test",
        "transaction-secret",
        "request-token",
        "request-api-key",
        "nested-token",
        "response-token",
        "response-cookie",
        "outside-secret",
    ):
        assert secret not in serialized

    trace_secrets = (
        "payer@example.test",
        "recipient@example.test",
        "transaction-secret",
        "request-token",
        "request-api-key",
        "nested-token",
        "response-token",
        "response-cookie",
    )
    with ZipFile(result.trace_path) as trace_zip:
        assert trace_zip.infolist()
        for entry in trace_zip.infolist():
            payload = entry.filename.encode("utf-8") + trace_zip.read(entry)
            for secret in trace_secrets:
                for variant in _secret_byte_variants(secret):
                    assert variant not in payload

    screenshot = Path(result.screenshot_paths[0])
    for box in (
        recipient_box,
        password_box,
        *repeated_secret_boxes,
    ):
        center_x = int(box["x"] + box["width"] / 2)
        center_y = int(box["y"] + box["height"] / 2)
        assert _png_pixel(screenshot, center_x, center_y) == (0, 0, 0)


def test_runner_supports_fixed_and_dynamic_amount_sources(
    tmp_path: Path,
) -> None:
    async def exercise() -> tuple[str, str, str]:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                browser_context = await browser.new_context()
                page = await browser_context.new_page()
                await page.set_content('<input data-testid="amount" />')
                context = RunnerContext(
                    page=page,
                    browser_context=browser_context,
                    artifacts_dir=tmp_path,
                    allowed_origin="https://wallet-test.local",
                    recipient_account="recipient@example.test",
                    transaction_password="transaction-secret",
                    valid_transfer_amount=Decimal("2.50"),
                    available_balance=Decimal("10.25"),
                )

                await execute_step(
                    DslTestStep(action="fill_amount", value="10"),
                    context,
                )
                fixed = await page.get_by_test_id("amount").input_value()
                await execute_step(
                    DslTestStep(
                        action="fill_amount",
                        source="valid_transfer_amount",
                    ),
                    context,
                )
                valid = await page.get_by_test_id("amount").input_value()
                await execute_step(
                    DslTestStep(
                        action="fill_amount",
                        source="amount_above_available_balance",
                    ),
                    context,
                )
                above_balance = (
                    await page.get_by_test_id("amount").input_value()
                )
                return fixed, valid, above_balance
            finally:
                await browser.close()

    assert asyncio.run(exercise()) == ("10", "2.50", "11.25")


@pytest.mark.parametrize(
    ("step", "context_field"),
    [
        (
            DslTestStep(
                action="fill_amount",
                source="valid_transfer_amount",
            ),
            "valid_transfer_amount",
        ),
        (
            DslTestStep(
                action="fill_amount",
                source="amount_above_available_balance",
            ),
            "available_balance",
        ),
    ],
)
def test_dynamic_amount_source_fails_when_context_is_missing(
    tmp_path: Path,
    step: DslTestStep,
    context_field: str,
) -> None:
    async def exercise() -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                browser_context = await browser.new_context()
                page = await browser_context.new_page()
                await page.set_content('<input data-testid="amount" />')
                context = RunnerContext(
                    page=page,
                    browser_context=browser_context,
                    artifacts_dir=tmp_path,
                    allowed_origin="https://wallet-test.local",
                    recipient_account="recipient@example.test",
                    transaction_password="transaction-secret",
                )
                with pytest.raises(
                    StepExecutionError,
                    match="required runner context is unavailable",
                ):
                    await execute_step(step, context)
                assert (
                    await page.get_by_test_id("amount").input_value()
                ) == ""
            finally:
                await browser.close()

    asyncio.run(exercise())
    assert context_field in {
        "valid_transfer_amount",
        "available_balance",
    }


def test_runner_failure_still_generates_screenshot_and_trace(
    tmp_path: Path,
) -> None:
    async def exercise():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                browser_context = await browser.new_context()
                page = await browser_context.new_page()
                page.set_default_timeout(100)
                await page.set_content("<main>No recipient input</main>")
                return await run_case(
                    _make_case(
                        "TC-OTI-003",
                        [
                            {
                                "action": "fill_recipient",
                                "source": "recipient_account",
                            }
                        ],
                    ),
                    RunnerContext(
                        page=page,
                        browser_context=browser_context,
                        artifacts_dir=tmp_path,
                        allowed_origin="https://wallet-test.local",
                        payer_account="payer@example.test",
                        recipient_account="recipient@example.test",
                        transaction_password="transaction-secret",
                    ),
                )
            finally:
                await browser.close()

    result = asyncio.run(exercise())

    assert result.status == "failed"
    assert result.error.endswith(": step execution failed")
    assert Path(result.screenshot_paths[0]).is_file()
    assert Path(result.trace_path).is_file()
    assert "recipient" not in result.error.lower()
    assert "payer@example.test" not in result.error
    assert "transaction-secret" not in result.error


def test_login_action_is_explicitly_rejected_and_evidence_is_saved(
    tmp_path: Path,
) -> None:
    async def exercise():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                browser_context = await browser.new_context()
                page = await browser_context.new_page()
                await page.set_content("<main>Already authenticated</main>")
                return await run_case(
                    _make_case(
                        "TC-LOGIN-001",
                        [{"action": "login"}],
                    ),
                    RunnerContext(
                        page=page,
                        browser_context=browser_context,
                        artifacts_dir=tmp_path,
                        allowed_origin="https://wallet-test.local",
                        recipient_account="recipient@example.test",
                        transaction_password="transaction-secret",
                    ),
                )
            finally:
                await browser.close()

    result = asyncio.run(exercise())

    assert result.status == "failed"
    assert result.error == "StepExecutionError: step execution failed"
    assert Path(result.screenshot_paths[0]).is_file()
    assert Path(result.trace_path).is_file()


def test_cleanup_failures_do_not_mask_original_step_failure(
    tmp_path: Path,
) -> None:
    class FakeLocator:
        pass

    class FailingTracing:
        async def start(self, **kwargs) -> None:
            return None

        async def stop(self, **kwargs) -> None:
            raise RuntimeError("trace-cleanup-secret")

    class FailingBrowserContext:
        tracing = FailingTracing()

    class FailingPage:
        def get_by_test_id(self, test_id: str) -> FakeLocator:
            return FakeLocator()

        def on(self, event: str, listener) -> None:
            return None

        def remove_listener(self, event: str, listener) -> None:
            raise RuntimeError("listener-cleanup-secret")

        async def screenshot(self, *, path: str, **kwargs) -> None:
            Path(path).write_bytes(b"partial-screenshot-cleanup-secret")
            raise RuntimeError("screenshot-cleanup-secret")

    result = asyncio.run(
        run_case(
            _make_case(
                "TC-CLEANUP-001",
                [{"action": "login"}],
            ),
            RunnerContext(
                page=FailingPage(),
                browser_context=FailingBrowserContext(),
                artifacts_dir=tmp_path,
                allowed_origin="https://wallet-test.local",
                recipient_account="recipient@example.test",
                transaction_password="transaction-secret",
            ),
        )
    )

    assert result.status == "failed"
    assert result.error == "StepExecutionError: step execution failed"
    assert "cleanup-secret" not in repr(result)
    assert not list(tmp_path.glob("*.png"))


def test_cancellation_during_trace_stop_waits_for_secret_cleanup(
    tmp_path: Path,
) -> None:
    raw_secret = "CANCELLED-RAW-TRACE-SECRET"

    async def exercise() -> None:
        stop_started = asyncio.Event()
        allow_stop_to_finish = asyncio.Event()

        class ControlledTracing:
            async def start(self, **kwargs) -> None:
                return None

            async def stop(self, *, path: str) -> None:
                with ZipFile(path, mode="w") as trace_zip:
                    trace_zip.writestr(
                        "trace.trace",
                        (
                            json.dumps(
                                {
                                    "type": "before",
                                    "callId": "call@cancel",
                                    "method": "fill",
                                    "params": {"value": raw_secret},
                                }
                            )
                            + "\n"
                            + json.dumps(
                                {
                                    "type": "after",
                                    "callId": "call@cancel",
                                }
                            )
                            + "\n"
                        ),
                    )
                stop_started.set()
                await allow_stop_to_finish.wait()

        class FakeBrowserContext:
            tracing = ControlledTracing()

        class FakeLocator:
            async def click(self) -> None:
                return None

        class FakePage:
            def get_by_test_id(self, test_id: str) -> FakeLocator:
                return FakeLocator()

            def get_by_text(
                self,
                text: str,
                *,
                exact: bool,
            ) -> FakeLocator:
                return FakeLocator()

            def on(self, event: str, listener) -> None:
                return None

            def remove_listener(self, event: str, listener) -> None:
                return None

            async def wait_for_timeout(self, timeout: int) -> None:
                return None

            async def screenshot(self, *, path: str, **kwargs) -> None:
                Path(path).write_bytes(b"masked-screenshot")

        task = asyncio.create_task(
            run_case(
                _make_case(
                    "TC-CANCEL-001",
                    [{"action": "submit"}],
                ),
                RunnerContext(
                    page=FakePage(),
                    browser_context=FakeBrowserContext(),
                    artifacts_dir=tmp_path,
                    allowed_origin="https://wallet-test.local",
                    recipient_account="recipient@example.test",
                    transaction_password=raw_secret,
                ),
            )
        )
        await asyncio.wait_for(stop_started.wait(), timeout=1)
        task.cancel()
        allow_stop_to_finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())

    assert not list(tmp_path.glob("*raw-trace*"))
    assert not list(tmp_path.glob("*sanitized-trace*"))
    for artifact in tmp_path.iterdir():
        assert raw_secret.encode() not in artifact.read_bytes()


def test_pre_run_cleanup_attempts_trace_and_screenshot_without_short_circuit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_artifact_path(
        artifacts_dir: Path,
        case_id: str,
        artifact_part: str,
    ) -> Path:
        return tmp_path / artifact_part

    def fake_remove(path: Path) -> bool:
        calls.append(path.name)
        return path.name != "trace.zip"

    monkeypatch.setattr(
        runner_module,
        "safe_artifact_path",
        fake_artifact_path,
    )
    monkeypatch.setattr(
        runner_module,
        "_remove_artifact",
        fake_remove,
    )

    with pytest.raises(
        runner_module.EvidenceCaptureError,
        match="evidence capture failed",
    ):
        asyncio.run(
            run_case(
                _make_case(
                    "TC-PRECLEAN-001",
                    [{"action": "submit"}],
                ),
                RunnerContext(
                    page=object(),
                    browser_context=object(),
                    artifacts_dir=tmp_path,
                    allowed_origin="https://wallet-test.local",
                    recipient_account="recipient@example.test",
                    transaction_password="transaction-secret",
                ),
            )
        )

    assert calls == ["trace.zip", "final.png"]


def test_run_case_rejects_unsafe_trace_and_removes_native_zip(
    tmp_path: Path,
) -> None:
    class UnsafeTracing:
        async def start(self, **kwargs) -> None:
            return None

        async def stop(self, *, path: str) -> None:
            with ZipFile(path, mode="w") as trace_zip:
                trace_zip.writestr(
                    "trace.trace",
                    json.dumps(
                        {
                            "type": "context-options",
                            "message": "Bearer UNKNOWN-RUNNER-TOKEN",
                        }
                    )
                    + "\n",
                )

    class FakeBrowserContext:
        tracing = UnsafeTracing()

    class FakeLocator:
        async def click(self) -> None:
            return None

    class FakePage:
        def get_by_test_id(self, test_id: str) -> FakeLocator:
            return FakeLocator()

        def on(self, event: str, listener) -> None:
            return None

        def remove_listener(self, event: str, listener) -> None:
            return None

        async def wait_for_timeout(self, timeout: int) -> None:
            return None

        async def screenshot(self, *, path: str, **kwargs) -> None:
            Path(path).write_bytes(b"masked-screenshot")

    result = asyncio.run(
        run_case(
            _make_case(
                "TC-TRACE-001",
                [{"action": "submit"}],
            ),
            RunnerContext(
                page=FakePage(),
                browser_context=FakeBrowserContext(),
                artifacts_dir=tmp_path,
                allowed_origin="https://wallet-test.local",
                recipient_account="recipient@example.test",
                transaction_password="transaction-secret",
            ),
        )
    )

    assert result.status == "failed"
    assert result.error == "EvidenceCaptureError: evidence capture failed"
    assert result.trace_path == ""
    assert not list(tmp_path.glob("*.zip"))
    assert not list(tmp_path.glob("*.tmp"))
    assert "UNKNOWN-RUNNER-TOKEN" not in repr(result)


def test_fixed_recipient_value_is_treated_as_sensitive_trace_data(
    tmp_path: Path,
) -> None:
    fixed_recipient = "fixed-recipient@example.test"

    class RecipientTracing:
        async def start(self, **kwargs) -> None:
            return None

        async def stop(self, *, path: str) -> None:
            with ZipFile(path, mode="w") as trace_zip:
                    trace_zip.writestr(
                        "trace.trace",
                        (
                            json.dumps(
                                {
                                    "type": "before",
                                    "callId": "call@recipient",
                                    "class": "Frame",
                                    "method": "fill",
                                    "params": {
                                        "value": fixed_recipient
                                    },
                                }
                            )
                            + "\n"
                            + json.dumps(
                                {
                                    "type": "after",
                                    "callId": "call@recipient",
                                }
                            )
                            + "\n"
                        )
                    )

    class FakeBrowserContext:
        tracing = RecipientTracing()

    class FakeLocator:
        async def fill(self, value: str) -> None:
            return None

    class FakePage:
        def get_by_test_id(self, test_id: str) -> FakeLocator:
            return FakeLocator()

        def on(self, event: str, listener) -> None:
            return None

        def remove_listener(self, event: str, listener) -> None:
            return None

        async def wait_for_timeout(self, timeout: int) -> None:
            return None

        async def screenshot(self, *, path: str, **kwargs) -> None:
            Path(path).write_bytes(b"masked-screenshot")

    result = asyncio.run(
        run_case(
            _make_case(
                "TC-FIXED-RECIPIENT-001",
                [
                    {
                        "action": "fill_recipient",
                        "value": fixed_recipient,
                    }
                ],
            ),
            RunnerContext(
                page=FakePage(),
                browser_context=FakeBrowserContext(),
                artifacts_dir=tmp_path,
                allowed_origin="https://wallet-test.local",
                recipient_account="different-recipient@example.test",
                transaction_password="transaction-secret",
            ),
        )
    )

    assert result.status == "completed"
    with ZipFile(result.trace_path) as trace_zip:
        for entry in trace_zip.infolist():
            assert fixed_recipient.encode() not in trace_zip.read(entry)


@pytest.mark.parametrize(
    "case_id",
    [
        "../TC-001",
        "TC/001",
        r"TC\001",
        ".",
        "..",
        "",
        "A" * 81,
        "TC 001",
    ],
)
def test_case_id_rejects_unsafe_artifact_names(case_id: str) -> None:
    with pytest.raises(ValueError, match="case_id"):
        validate_case_id(case_id)


def test_artifact_path_cannot_escape_artifacts_directory(
    tmp_path: Path,
) -> None:
    artifacts_dir = tmp_path / "artifacts"
    outside = tmp_path / "outside.png"
    artifacts_dir.mkdir()
    (artifacts_dir / "TC-001-final.png").symlink_to(outside)

    with pytest.raises(ValueError, match="artifact"):
        safe_artifact_path(
            artifacts_dir,
            "TC-001",
            "final.png",
        )


def test_artifacts_directory_is_private_after_creation(
    tmp_path: Path,
) -> None:
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir(mode=0o777)
    artifacts_dir.chmod(0o777)

    safe_artifact_path(
        artifacts_dir,
        "TC-PRIVATE-001",
        "final.png",
    )

    assert artifacts_dir.stat().st_mode & 0o777 == 0o700


def test_runner_waits_for_delayed_same_origin_request(
    tmp_path: Path,
) -> None:
    async def exercise():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                browser_context = await browser.new_context()
                page = await browser_context.new_page()

                async def delayed_route(route) -> None:
                    await asyncio.sleep(0.6)
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body='{"status":"ok"}',
                    )

                await page.route(
                    "https://wallet-test.local/**",
                    delayed_route,
                )
                await page.set_content(
                    """
                    <button data-testid="submit-transfer"
                      onclick="
                        fetch('https://wallet-test.local/api/delayed');
                      ">
                      Submit
                    </button>
                    """
                )
                return await run_case(
                    _make_case(
                        "TC-DELAYED-001",
                        [{"action": "submit"}],
                    ),
                    RunnerContext(
                        page=page,
                        browser_context=browser_context,
                        artifacts_dir=tmp_path,
                        allowed_origin="https://wallet-test.local",
                        recipient_account="recipient@example.test",
                        transaction_password="transaction-secret",
                    ),
                )
            finally:
                await browser.close()

    result = asyncio.run(exercise())

    assert result.status == "completed"
    assert len(result.network_inventory) == 1
    assert result.network_inventory[0].path == "/api/delayed"
    assert result.network_inventory[0].status == 200


def test_runner_fails_safely_when_same_origin_request_never_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runner_module,
        "NETWORK_IDLE_TIMEOUT_SECONDS",
        0.05,
        raising=False,
    )

    async def exercise():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                browser_context = await browser.new_context()
                page = await browser_context.new_page()

                async def delayed_route(route) -> None:
                    await asyncio.sleep(0.6)
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body='{"status":"late"}',
                    )

                await page.route(
                    "https://wallet-test.local/**",
                    delayed_route,
                )
                await page.set_content(
                    """
                    <button data-testid="submit-transfer"
                      onclick="
                        fetch('https://wallet-test.local/api/timeout');
                      ">
                      Submit
                    </button>
                    """
                )
                return await run_case(
                    _make_case(
                        "TC-TIMEOUT-001",
                        [{"action": "submit"}],
                    ),
                    RunnerContext(
                        page=page,
                        browser_context=browser_context,
                        artifacts_dir=tmp_path,
                        allowed_origin="https://wallet-test.local",
                        recipient_account="recipient@example.test",
                        transaction_password="transaction-secret",
                    ),
                )
            finally:
                await browser.close()

    result = asyncio.run(exercise())

    assert result.status == "failed"
    assert result.error == "EvidenceCaptureError: evidence capture failed"
    assert result.network_inventory == ()


def test_network_capture_honors_entry_limit_and_removes_listener(
    tmp_path: Path,
) -> None:
    async def exercise() -> tuple[int, int]:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                browser_context = await browser.new_context()
                page = await browser_context.new_page()

                async def route_handler(route) -> None:
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body='{"status":"ok"}',
                    )

                await page.route(
                    "https://wallet-test.local/**",
                    route_handler,
                )
                await page.set_content(
                    """
                    <button data-testid="submit-transfer"
                      onclick="
                        for (let i = 0; i < 5; i++) {
                          fetch('https://wallet-test.local/api/' + i);
                        }
                      ">
                      Submit
                    </button>
                    """
                )
                context = RunnerContext(
                    page=page,
                    browser_context=browser_context,
                    artifacts_dir=tmp_path,
                    allowed_origin="https://wallet-test.local",
                    recipient_account="recipient@example.test",
                    transaction_password="transaction-secret",
                    max_network_entries=2,
                )
                first = await run_case(
                    _make_case(
                        "TC-CAP-001",
                        [{"action": "submit"}],
                    ),
                    context,
                )
                second = await run_case(
                    _make_case(
                        "TC-CAP-002",
                        [{"action": "submit"}],
                    ),
                    context,
                )
                return (
                    len(first.network_inventory),
                    len(second.network_inventory),
                )
            finally:
                await browser.close()

    assert asyncio.run(exercise()) == (2, 2)


def test_runner_source_contains_no_dynamic_code_execution() -> None:
    source = Path("agent_service/execution/runner.py").read_text(
        encoding="utf-8"
    )

    for forbidden in (
        "eval(",
        "exec(",
        "page.evaluate(",
        "subprocess",
    ):
        assert forbidden not in source
