from __future__ import annotations

from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    task_id: str
    thread_id: str
    user_message: str
    status: str
    source_versions: list[dict[str, str]]
    source_text: str
    requirements: dict[str, Any]
    risks: dict[str, Any]
    related_bugs: list[dict[str, Any]]
    test_plan: dict[str, Any]
    approval: dict[str, Any]
    errors: list[str]
