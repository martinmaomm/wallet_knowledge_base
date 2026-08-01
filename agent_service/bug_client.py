from __future__ import annotations

import math
from numbers import Real
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from agent_service.schemas import RelatedBug


LOCAL_BUG_SERVICE_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
BUG_SERVICE_SCHEMES = frozenset({"http", "https"})
MAX_BUG_QUERIES = 10
MAX_QUERY_CHARACTERS = 200
MAX_TOTAL_QUERY_CHARACTERS = 2000


class BugClientError(RuntimeError):
    """Raised when the local Bug service returns an unusable response."""


def _validate_base_url(base_url: str) -> str:
    if not isinstance(base_url, str) or base_url != base_url.strip():
        raise ValueError("base_url must be a local HTTP origin")

    parsed = None
    parse_failed = False
    try:
        parsed = urlsplit(base_url)
        parsed.port
    except (TypeError, ValueError):
        parse_failed = True

    if parse_failed or parsed is None:
        raise ValueError("base_url must be a local HTTP origin")
    hostname = parsed.hostname

    if (
        parsed.scheme.lower() not in BUG_SERVICE_SCHEMES
        or hostname not in LOCAL_BUG_SERVICE_HOSTS
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.path)
        or bool(parsed.query)
        or bool(parsed.fragment)
    ):
        raise ValueError("base_url must be a local HTTP origin")
    return base_url


def _normalize_queries(queries: list[str]) -> list[str]:
    if not isinstance(queries, list):
        raise ValueError("bug queries must be a list of strings")
    if len(queries) > MAX_BUG_QUERIES:
        raise ValueError("bug queries must contain at most 10 entries")
    if any(not isinstance(query, str) for query in queries):
        raise ValueError("bug queries must contain only strings")
    if sum(len(query) for query in queries) > MAX_TOTAL_QUERY_CHARACTERS:
        raise ValueError("bug queries must contain at most 2000 total characters")

    normalized: list[str] = []
    seen: set[str] = set()

    for query in queries:
        stripped = query.strip()
        if not stripped or stripped in seen:
            continue
        if len(stripped) > MAX_QUERY_CHARACTERS:
            raise ValueError("each bug query must be at most 200 characters")
        seen.add(stripped)
        normalized.append(stripped)

    return normalized


def _validate_timeout(
    timeout: float | httpx.Timeout,
) -> float | httpx.Timeout:
    if isinstance(timeout, bool):
        raise ValueError("timeout values must be finite positive numbers")

    if isinstance(timeout, httpx.Timeout):
        values = tuple(timeout.as_dict().values())
    elif isinstance(timeout, Real):
        values = (timeout,)
    else:
        raise ValueError("timeout values must be finite positive numbers")

    if any(
        value is None
        or isinstance(value, bool)
        or not isinstance(value, Real)
        or not math.isfinite(float(value))
        or value <= 0
        for value in values
    ):
        raise ValueError("timeout values must be finite positive numbers")
    return timeout


async def _request_bugs(
    client: httpx.AsyncClient,
    query: str,
) -> tuple[httpx.Response | None, bool]:
    try:
        response = await client.get(
            "/bugs",
            params={"keyword": query, "limit": 20},
        )
    except httpx.RequestError:
        return None, True
    return response, False


def _decode_json(response: httpx.Response) -> tuple[object, bool]:
    try:
        payload = response.json()
    except (ValueError, UnicodeDecodeError):
        return None, True
    return payload, False


def _parse_related_bug(row: object) -> tuple[RelatedBug | None, bool]:
    projected = {
        field: row.get(field) if isinstance(row, dict) else None
        for field in (
            "bug_id",
            "title",
            "severity",
            "status",
            "resolution",
        )
    }
    try:
        bug = RelatedBug.model_validate(projected)
    except ValidationError:
        return None, True
    return bug, False


class BugClient:
    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float | httpx.Timeout = 10.0,
    ) -> None:
        self.base_url = _validate_base_url(base_url)
        self.transport = transport
        self.timeout = _validate_timeout(timeout)

    async def search_related(self, queries: list[str]) -> list[RelatedBug]:
        normalized_queries = _normalize_queries(queries)
        if not normalized_queries:
            return []

        bugs_by_id: dict[int, RelatedBug] = {}
        async with httpx.AsyncClient(
            base_url=self.base_url,
            transport=self.transport,
            timeout=self.timeout,
            trust_env=False,
        ) as client:
            for query in normalized_queries:
                response, request_failed = await _request_bugs(client, query)
                if request_failed or response is None:
                    raise BugClientError("bug service request failed")
                if not response.is_success:
                    raise BugClientError(
                        f"bug service returned HTTP {response.status_code}"
                    )

                payload, json_failed = _decode_json(response)
                if json_failed:
                    raise BugClientError("bug service returned invalid JSON")
                if (
                    not isinstance(payload, dict)
                    or not isinstance(payload.get("bugs"), list)
                ):
                    raise BugClientError(
                        "bug service returned an invalid container"
                    )

                for row in payload["bugs"]:
                    bug, record_failed = _parse_related_bug(row)
                    if record_failed or bug is None:
                        raise BugClientError(
                            "bug service returned an invalid bug record"
                        )

                    existing = bugs_by_id.get(bug.bug_id)
                    if existing is not None and existing != bug:
                        raise BugClientError(
                            "bug service returned conflicting records"
                        )
                    bugs_by_id[bug.bug_id] = bug

        return sorted(
            bugs_by_id.values(),
            key=lambda bug: bug.bug_id,
            reverse=True,
        )
