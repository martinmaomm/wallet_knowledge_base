from __future__ import annotations

import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any

from .models import BugRecord


EMPTY_DATES = {"", "0000-00-00", "0000-00-00 00:00:00"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"br", "p", "div", "li", "tr"}:
            self.parts.append("\n")
        if tag == "img":
            alt = dict(attrs).get("alt")
            self.parts.append(f"[图片: {alt}]" if alt else "[图片]")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


def html_to_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parser = _TextExtractor()
    parser.feed(text)
    parser.close()
    return parser.text()


def normalize_user(value: Any) -> str:
    if isinstance(value, dict):
        account = str(value.get("account") or "").strip()
        realname = str(value.get("realname") or "").strip()
        if realname and account and realname != account:
            return f"{realname} ({account})"
        return realname or account
    return str(value or "").strip()


def normalize_date(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text in EMPTY_DATES else text


def optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def is_reopened(bug: dict[str, Any]) -> bool:
    for key in ("activatedCount", "reopenedCount"):
        value = bug.get(key)
        if value not in (None, ""):
            try:
                return int(value) > 0
            except (TypeError, ValueError):
                return bool(value)
    for key in ("reactivatedDate", "activatedDate"):
        if normalize_date(bug.get(key)):
            return True
    return False


def normalize_bug(
    bug: dict[str, Any],
    *,
    default_product: str,
    module_names: dict[int, str] | None = None,
    synced_at: str | None = None,
) -> BugRecord:
    bug_id = optional_int(bug.get("id"))
    if bug_id is None:
        raise ValueError("ZenTao bug payload is missing a numeric id")

    module_id = optional_int(bug.get("module")) or 0
    module_title = str(bug.get("moduleTitle") or "").strip()
    if not module_title and module_names:
        module_title = module_names.get(module_id, "")
    if not module_title:
        module_title = "未设置" if module_id == 0 else f"模块ID {module_id}"

    return BugRecord(
        bug_id=bug_id,
        product=str(bug.get("productName") or default_product).strip(),
        module=module_title,
        title=str(bug.get("title") or "").replace("\r", " ").replace("\n", " ").strip(),
        severity=optional_int(bug.get("severity")),
        priority=optional_int(bug.get("pri") if "pri" in bug else bug.get("priority")),
        status=str(bug.get("status") or "").strip(),
        bug_type=str(bug.get("type") or "").strip(),
        reproduction_steps=html_to_text(bug.get("steps")),
        created_by=normalize_user(bug.get("openedBy")),
        assigned_to=normalize_user(bug.get("assignedTo")),
        created_at=normalize_date(bug.get("openedDate") or bug.get("createdDate")),
        resolved_by=normalize_user(bug.get("resolvedBy")),
        resolution=str(bug.get("resolution") or "").strip(),
        resolved_at=normalize_date(bug.get("resolvedDate")),
        closed_at=normalize_date(bug.get("closedDate")),
        is_reopened=is_reopened(bug),
        synced_at=synced_at or datetime.now(UTC).isoformat(),
    )

