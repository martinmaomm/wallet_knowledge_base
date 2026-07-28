from pathlib import Path

import pytest

from bug_service.db import BugRepository
from bug_service.models import BugRecord


def make_bug(bug_id: int, title: str, status: str = "active") -> BugRecord:
    return BugRecord(
        bug_id=bug_id,
        product="内部钱包",
        module="Web2",
        title=title,
        severity=2,
        priority=3,
        status=status,
        bug_type="codeerror",
        reproduction_steps="步骤",
        created_by="马丁",
        assigned_to="开发",
        created_at="2026-01-01T00:00:00Z",
        resolved_by="",
        resolution="",
        resolved_at="",
        closed_at="",
        is_reopened=False,
        synced_at="2026-07-28T00:00:00+00:00",
    )


def test_replace_get_and_search(tmp_path: Path) -> None:
    repository = BugRepository(tmp_path / "bugs.sqlite3")
    repository.initialize()
    assert repository.replace_all([make_bug(1, "充值异常"), make_bug(2, "提现异常", "closed")]) == 2

    assert repository.get(1)["title"] == "充值异常"
    assert [item["bug_id"] for item in repository.search(keyword="提现")] == [2]
    assert [item["bug_id"] for item in repository.search(status="closed")] == [2]

    repository.replace_all([make_bug(3, "新数据")])
    assert repository.get(1) is None
    assert repository.stats()["bug_count"] == 1


def test_replace_all_rolls_back_on_duplicate_ids(tmp_path: Path) -> None:
    repository = BugRepository(tmp_path / "bugs.sqlite3")
    repository.initialize()
    repository.replace_all([make_bug(1, "原始数据")])

    with pytest.raises(Exception):
        repository.replace_all([make_bug(2, "重复一"), make_bug(2, "重复二")])

    assert repository.get(1)["title"] == "原始数据"

