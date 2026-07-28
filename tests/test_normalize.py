from bug_service.normalize import normalize_bug


def test_normalize_bug_extracts_requested_fields() -> None:
    record = normalize_bug(
        {
            "id": "1227",
            "title": "Example",
            "severity": "2",
            "pri": "3",
            "status": "closed",
            "type": "codeerror",
            "steps": "<p>[步骤]</p><p>点击提交</p><p><img alt=\"result.png\" /></p>",
            "openedBy": {"account": "martin", "realname": "马丁"},
            "assignedTo": None,
            "openedDate": "2026-02-09T09:38:04Z",
            "resolvedBy": {"account": "developer"},
            "resolution": "fixed",
            "resolvedDate": "2026-03-03T10:35:54Z",
            "closedDate": "2026-03-03T10:35:58Z",
            "activatedCount": 1,
            "module": 0,
        },
        default_product="内部钱包",
        synced_at="2026-07-28T00:00:00+00:00",
    )

    assert record.bug_id == 1227
    assert record.module == "未设置"
    assert record.severity == 2
    assert record.priority == 3
    assert record.created_by == "马丁 (martin)"
    assert record.assigned_to == ""
    assert record.resolution == "fixed"
    assert record.is_reopened is True
    assert record.reproduction_steps == "[步骤]\n点击提交\n[图片: result.png]"

