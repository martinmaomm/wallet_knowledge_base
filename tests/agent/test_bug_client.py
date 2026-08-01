from __future__ import annotations

import asyncio
from collections.abc import Callable
import math

import httpx
import pytest

from agent_service.bug_client import BugClient, BugClientError


def run_search(
    handler: Callable[[httpx.Request], httpx.Response],
    queries: list[str],
    *,
    base_url: str = "http://127.0.0.1:8765",
    timeout: float | httpx.Timeout = 10.0,
):
    client = BugClient(
        base_url,
        transport=httpx.MockTransport(handler),
        timeout=timeout,
    )
    return asyncio.run(client.search_related(queries))


def full_bug(
    bug_id: int,
    *,
    title: str | None = None,
    severity: int | None = 2,
) -> dict[str, object]:
    return {
        "bug_id": bug_id,
        "product": "钱包",
        "module": "Web2 内部转账",
        "title": title or f"Bug {bug_id}",
        "severity": severity,
        "severity_label": "严重 (2)",
        "priority": 1,
        "status": "closed",
        "status_label": "已关闭 (closed)",
        "bug_type": "codeerror",
        "bug_type_label": "代码错误 (codeerror)",
        "reproduction_steps": "步骤",
        "created_by": "tester",
        "assigned_to": "developer",
        "created_at": "2026-07-01T00:00:00Z",
        "resolved_by": "developer",
        "resolution": "fixed",
        "resolution_label": "已修复 (fixed)",
        "resolved_at": "2026-07-02T00:00:00Z",
        "closed_at": "2026-07-03T00:00:00Z",
        "is_reopened": False,
        "synced_at": "2026-07-28T00:00:00Z",
    }


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:8765",
        "https://127.0.0.1:8765",
        "http://[::1]:8765",
    ],
)
def test_bug_client_accepts_only_explicit_local_origins(base_url: str) -> None:
    client = BugClient(base_url, transport=httpx.MockTransport(lambda request: None))

    assert client.base_url == base_url


@pytest.mark.parametrize(
    "base_url",
    [
        "ftp://localhost:8765",
        "http://bug-service:8765",
        "http://127.0.0.2:8765",
        "http://user@localhost:8765",
        "http://localhost:8765/",
        "http://localhost:8765/bugs",
        "http://localhost:8765?debug=1",
        "http://localhost:8765#fragment",
        "http://localhost:not-a-port",
        "http://%6cocalhost:8765",
    ],
)
def test_bug_client_rejects_non_local_or_non_origin_urls(base_url: str) -> None:
    with pytest.raises(ValueError, match="local HTTP origin"):
        BugClient(base_url)


def test_bug_client_maps_full_responses_deduplicates_and_sorts() -> None:
    seen_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_queries.append(request.url.params["keyword"])
        assert request.url.path == "/bugs"
        assert request.url.params["limit"] == "20"
        if request.url.params["keyword"] == "内部转账":
            bugs = [full_bug(1227), full_bug(1300)]
        else:
            bugs = [full_bug(1227), full_bug(900, severity=None)]
        return httpx.Response(200, json={"count": len(bugs), "bugs": bugs})

    bugs = run_search(handler, [" 内部转账 ", "重复提交", "内部转账"])

    assert seen_queries == ["内部转账", "重复提交"]
    assert [item.bug_id for item in bugs] == [1300, 1227, 900]
    assert bugs[0].model_dump() == {
        "bug_id": 1300,
        "title": "Bug 1300",
        "severity": 2,
        "status": "closed",
        "resolution": "fixed",
    }
    assert bugs[-1].severity is None


def test_bug_client_deduplicates_identical_bug_records() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"count": 1, "bugs": [full_bug(1227)]},
        )

    bugs = run_search(handler, ["内部转账", "重复提交"])

    assert [bug.bug_id for bug in bugs] == [1227]


def test_bug_client_rejects_conflicting_bug_records_regardless_of_order() -> None:
    records = {
        "first": full_bug(1227, title="first-secret"),
        "second": full_bug(1227, title="second-secret"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["keyword"]
        return httpx.Response(
            200,
            json={"count": 1, "bugs": [records[query]]},
        )

    for queries in (["first", "second"], ["second", "first"]):
        with pytest.raises(BugClientError) as caught:
            run_search(handler, queries)

        assert str(caught.value) == "bug service returned conflicting records"
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
        assert "first-secret" not in str(caught.value)
        assert "second-secret" not in str(caught.value)


def test_bug_client_empty_queries_do_not_send_requests() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    bugs = run_search(handler, ["", "  ", "\t"])

    assert bugs == []
    assert calls == 0


def test_bug_client_applies_default_and_injected_timeout() -> None:
    seen_timeouts: list[dict[str, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_timeouts.append(request.extensions["timeout"])
        return httpx.Response(200, json={"count": 0, "bugs": []})

    run_search(handler, ["first"])
    run_search(handler, ["second"], timeout=2.5)

    assert set(seen_timeouts[0].values()) == {10.0}
    assert set(seen_timeouts[1].values()) == {2.5}


def test_bug_client_disables_environment_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:8080")
    captured_options: dict[str, object] = {}
    real_async_client = httpx.AsyncClient

    def capturing_async_client(
        *args: object,
        **kwargs: object,
    ) -> httpx.AsyncClient:
        captured_options.update(kwargs)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", capturing_async_client)

    bugs = run_search(
        lambda request: httpx.Response(
            200,
            json={"count": 0, "bugs": []},
        ),
        ["internal transfer"],
    )

    assert bugs == []
    assert captured_options["trust_env"] is False


def test_bug_client_accepts_complete_positive_httpx_timeout() -> None:
    timeout = httpx.Timeout(connect=1.0, read=2.0, write=3.0, pool=4.0)
    seen_timeout: dict[str, float] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_timeout.update(request.extensions["timeout"])
        return httpx.Response(200, json={"count": 0, "bugs": []})

    run_search(handler, ["timeout"], timeout=timeout)

    assert seen_timeout == {
        "connect": 1.0,
        "read": 2.0,
        "write": 3.0,
        "pool": 4.0,
    }


@pytest.mark.parametrize(
    "timeout",
    [
        None,
        True,
        False,
        0,
        -1,
        math.nan,
        math.inf,
        -math.inf,
        httpx.Timeout(connect=None, read=1.0, write=1.0, pool=1.0),
        httpx.Timeout(connect=1.0, read=math.nan, write=1.0, pool=1.0),
        httpx.Timeout(connect=1.0, read=1.0, write=math.inf, pool=1.0),
        httpx.Timeout(connect=1.0, read=1.0, write=1.0, pool=-1.0),
    ],
)
def test_bug_client_rejects_invalid_timeout_values(
    timeout: object,
) -> None:
    with pytest.raises(ValueError, match="finite positive"):
        BugClient("http://127.0.0.1:8765", timeout=timeout)  # type: ignore[arg-type]


def test_bug_client_accepts_ten_unique_queries_of_200_characters() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert len(request.url.params["keyword"]) == 200
        return httpx.Response(200, json={"count": 0, "bugs": []})

    queries = [f"{index:02d}" + ("x" * 198) for index in range(10)]

    assert run_search(handler, queries) == []
    assert calls == 10


@pytest.mark.parametrize(
    ("queries", "expected_message"),
    [
        ([str(index) for index in range(11)], "at most 10"),
        (["duplicate"] * 11, "at most 10"),
        ([("x" * 199) + "  "] * 10, "at most 2000"),
        (["x" * 201], "at most 200 characters"),
        (["valid", 1227], "strings"),
    ],
)
def test_bug_client_rejects_query_limits_before_requesting(
    queries: list[object],
    expected_message: str,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"count": 0, "bugs": []})

    with pytest.raises(ValueError, match=expected_message):
        run_search(handler, queries)  # type: ignore[arg-type]

    assert calls == 0


def test_bug_client_checks_raw_count_before_normalizing_queries() -> None:
    class ExplodingQuery(str):
        def strip(self, chars: str | None = None) -> str:
            raise AssertionError("queries were traversed before raw count check")

    queries = [ExplodingQuery("duplicate")] * 11

    with pytest.raises(ValueError, match="at most 10"):
        run_search(lambda request: httpx.Response(500), queries)


def assert_sanitized_error(
    caught: pytest.ExceptionInfo[BugClientError],
    *,
    expected_message: str,
) -> None:
    error = caught.value
    assert str(error) == expected_message
    assert error.__cause__ is None
    assert error.__context__ is None
    assert "query-secret" not in str(error)
    assert "response-secret" not in str(error)


def test_bug_client_sanitizes_http_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "response-secret"})

    with pytest.raises(BugClientError) as caught:
        run_search(handler, ["query-secret"])

    assert_sanitized_error(
        caught,
        expected_message="bug service returned HTTP 503",
    )


def test_bug_client_sanitizes_network_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            "response-secret",
            request=request,
        )

    with pytest.raises(BugClientError) as caught:
        run_search(handler, ["query-secret"])

    assert_sanitized_error(
        caught,
        expected_message="bug service request failed",
    )


def test_bug_client_sanitizes_json_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"response-secret")

    with pytest.raises(BugClientError) as caught:
        run_search(handler, ["query-secret"])

    assert_sanitized_error(
        caught,
        expected_message="bug service returned invalid JSON",
    )


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"bugs": {}},
        {"bugs": "not-a-list"},
    ],
)
def test_bug_client_rejects_invalid_response_containers(
    payload: object,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with pytest.raises(BugClientError) as caught:
        run_search(handler, ["query-secret"])

    assert_sanitized_error(
        caught,
        expected_message="bug service returned an invalid container",
    )


def test_bug_client_sanitizes_related_bug_schema_errors() -> None:
    invalid_bug = full_bug(1227)
    invalid_bug["severity"] = "response-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"count": 1, "bugs": [invalid_bug]})

    with pytest.raises(BugClientError) as caught:
        run_search(handler, ["query-secret"])

    assert_sanitized_error(
        caught,
        expected_message="bug service returned an invalid bug record",
    )
