from __future__ import annotations

import os
import re
from html import escape
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from agent_service.artifacts import atomic_write_json, atomic_write_text
from agent_service.dsl import REQUIRED_BASELINE_IDS


_TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_STATUSES = frozenset(
    {
        "completed",
        "failure_classified",
        "reject",
        "supplement",
        "cancel",
    }
)


class CoverageMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    coverage_percent: float
    covered_case_ids: tuple[str, ...]
    missing_case_ids: tuple[str, ...]


def evaluate_golden_set(case_ids: list[str]) -> CoverageMetrics:
    supplied = {
        item for item in case_ids if isinstance(item, str)
    }
    covered = tuple(sorted(REQUIRED_BASELINE_IDS.intersection(supplied)))
    missing = tuple(sorted(REQUIRED_BASELINE_IDS.difference(supplied)))
    percentage = len(covered) / len(REQUIRED_BASELINE_IDS) * 100
    return CoverageMetrics(
        coverage_percent=percentage,
        covered_case_ids=covered,
        missing_case_ids=missing,
    )


def _validate_task_id(task_id: str) -> str:
    if (
        not isinstance(task_id, str)
        or task_id in {"", ".", ".."}
        or _TASK_ID_PATTERN.fullmatch(task_id) is None
    ):
        raise ValueError("task_id contains unsafe characters")
    return task_id


def _safe_cases(state: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    raw_plan = state.get("test_plan")
    if not isinstance(raw_plan, dict):
        return "", []
    summary = raw_plan.get("summary")
    safe_summary = summary if isinstance(summary, str) else ""
    raw_cases = raw_plan.get("cases")
    if not isinstance(raw_cases, list):
        return safe_summary, []

    cases: list[dict[str, Any]] = []
    for item in raw_cases:
        if not isinstance(item, dict):
            continue
        case_id = item.get("case_id")
        title = item.get("title")
        priority = item.get("priority")
        if not all(isinstance(value, str) for value in (case_id, title, priority)):
            continue
        cases.append(
            {
                "case_id": case_id,
                "title": title,
                "priority": priority,
            }
        )
    return safe_summary, cases


def _safe_list(state: dict[str, Any], name: str) -> list[dict[str, Any]]:
    value = state.get(name)
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _safe_failure_analysis(state: dict[str, Any]) -> dict[str, Any] | None:
    value = state.get("failure_analysis")
    return dict(value) if isinstance(value, dict) else None


def _markdown_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def write_reports(
    *,
    task_id: str,
    artifacts_root: Path,
    state: dict[str, Any],
) -> dict[str, str]:
    safe_task_id = _validate_task_id(task_id)
    root = Path(artifacts_root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    resolved_root = root.resolve(strict=True)
    directory = (resolved_root / safe_task_id).resolve(strict=False)
    if directory.parent != resolved_root:
        raise ValueError("task_id escapes artifacts root")
    directory.mkdir(mode=0o700)
    os.chmod(directory, 0o700)

    summary, cases = _safe_cases(state)
    metrics = evaluate_golden_set(
        [item["case_id"] for item in cases]
    )
    status = state.get("status")
    safe_status = status if status in _SAFE_STATUSES else "unknown"
    passed = state.get("passed")
    safe_passed = passed if type(passed) is bool else None
    assertions = _safe_list(state, "assertion_results")
    executions = _safe_list(state, "execution_results")
    failure = _safe_failure_analysis(state)
    payload = {
        "task_id": safe_task_id,
        "status": safe_status,
        "passed": safe_passed,
        "coverage": metrics.model_dump(mode="json"),
        "test_plan": {"summary": summary, "cases": cases},
        "assertion_results": assertions,
        "execution_results": executions,
        "failure_analysis": failure,
    }

    json_path = directory / "execution_results.json"
    markdown_path = directory / "report.md"
    html_path = directory / "report.html"
    atomic_write_json(json_path, payload)

    failure_summary = ""
    if failure is not None and isinstance(failure.get("summary"), str):
        failure_summary = failure["summary"]
    markdown = (
        f"# AI Test Agent Report: {_markdown_text(safe_task_id)}\n\n"
        f"- Status: {_markdown_text(safe_status)}\n"
        f"- Passed: {safe_passed}\n"
        f"- Golden Set Coverage: {metrics.coverage_percent:.0f}%\n"
        f"- Cases: {len(cases)}\n\n"
        f"## Plan Summary\n\n{_markdown_text(summary)}\n\n"
        f"## Failure Summary\n\n{_markdown_text(failure_summary)}\n"
    )
    atomic_write_text(markdown_path, markdown)
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{escape(safe_task_id)}</title></head><body>"
        f"<h1>AI Test Agent Report: {escape(safe_task_id)}</h1>"
        f"<p>Status: {escape(safe_status)}</p>"
        f"<p>Golden Set Coverage: {metrics.coverage_percent:.0f}%</p>"
        f"<h2>Plan Summary</h2><p>{escape(summary)}</p>"
        f"<h2>Failure Summary</h2><p>{escape(failure_summary)}</p>"
        "</body></html>"
    )
    atomic_write_text(html_path, html)
    return {
        "json": str(json_path),
        "markdown": str(markdown_path),
        "html": str(html_path),
    }
