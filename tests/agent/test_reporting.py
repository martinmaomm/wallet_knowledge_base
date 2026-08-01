from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_service.artifacts import atomic_write_json
from agent_service.reporting import evaluate_golden_set, write_reports


GOLDEN_IDS = [f"TC-OTI-{index:03d}" for index in range(1, 7)]


def test_golden_set_coverage_is_measurable_and_ignores_duplicates() -> None:
    complete = evaluate_golden_set(GOLDEN_IDS + [GOLDEN_IDS[0], "EXTRA-1"])
    partial = evaluate_golden_set(GOLDEN_IDS[:4])

    assert complete.coverage_percent == 100
    assert complete.missing_case_ids == ()
    assert complete.covered_case_ids == tuple(GOLDEN_IDS)
    assert partial.coverage_percent == pytest.approx(66.6666666667)
    assert partial.missing_case_ids == ("TC-OTI-005", "TC-OTI-006")


def test_atomic_json_write_replaces_content_and_uses_private_permissions(
    tmp_path: Path,
) -> None:
    target = tmp_path / "nested" / "result.json"
    atomic_write_json(target, {"status": "first"})
    atomic_write_json(target, {"status": "second"})

    assert json.loads(target.read_text(encoding="utf-8")) == {
        "status": "second"
    }
    assert os.stat(target).st_mode & 0o777 == 0o600
    assert not list(target.parent.glob("*.tmp-*"))


def test_atomic_json_rejects_symlink_target(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text('{"safe":true}', encoding="utf-8")
    target = tmp_path / "result.json"
    target.symlink_to(outside)

    with pytest.raises(ValueError, match="regular file"):
        atomic_write_json(target, {"unsafe": True})

    assert outside.read_text(encoding="utf-8") == '{"safe":true}'


def test_reports_are_safe_private_and_human_readable(tmp_path: Path) -> None:
    state = {
        "status": "failure_classified",
        "passed": False,
        "source_text": "PRD-SECRET transaction-password-secret",
        "user_message": "SECRET USER PROMPT",
        "test_plan": {
            "summary": "<script>alert('x')</script>",
            "cases": [
                {
                    "case_id": case_id,
                    "title": f"Case <{case_id}>",
                    "priority": "P0",
                }
                for case_id in GOLDEN_IDS
            ],
        },
        "assertion_results": [
            {
                "name": "balance_change",
                "passed": False,
                "expected": "-10",
                "actual": "0",
            }
        ],
        "failure_analysis": {
            "category": "product",
            "summary": "Balance did not change",
            "evidence_refs": ["deterministic_assertion_results"],
            "related_bug_ids": [1227],
            "recommended_action": "Review balance update",
        },
    }

    paths = write_reports(
        task_id="TASK-abc123",
        artifacts_root=tmp_path,
        state=state,
    )

    assert set(paths) == {"json", "markdown", "html"}
    for path_text in paths.values():
        path = Path(path_text)
        assert path.is_file()
        assert path.resolve().is_relative_to(tmp_path.resolve())
        assert os.stat(path).st_mode & 0o777 == 0o600

    payload = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
    assert set(payload) == {
        "task_id",
        "status",
        "passed",
        "coverage",
        "test_plan",
        "assertion_results",
        "execution_results",
        "failure_analysis",
    }
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "PRD-SECRET" not in serialized
    assert "transaction-password-secret" not in serialized
    assert "SECRET USER PROMPT" not in serialized
    assert payload["coverage"]["coverage_percent"] == 100

    markdown = Path(paths["markdown"]).read_text(encoding="utf-8")
    html = Path(paths["html"]).read_text(encoding="utf-8")
    assert "Golden Set Coverage: 100%" in markdown
    assert "Balance did not change" in markdown
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "PRD-SECRET" not in markdown + html


@pytest.mark.parametrize("task_id", ["../escape", "TASK/escape", "", ".", ".."])
def test_report_task_id_cannot_escape_artifacts_root(
    tmp_path: Path,
    task_id: str,
) -> None:
    with pytest.raises(ValueError, match="task_id"):
        write_reports(
            task_id=task_id,
            artifacts_root=tmp_path,
            state={"status": "completed", "test_plan": {"cases": []}},
        )
