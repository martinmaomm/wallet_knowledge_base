import asyncio
from pathlib import Path

import httpx

from bug_service.api import create_app
from bug_service.db import BugRepository
from bug_service.models import BugRecord


def test_api_exact_lookup_and_not_found(tmp_path: Path) -> None:
    database = tmp_path / "bugs.sqlite3"
    repository = BugRepository(database)
    repository.initialize()
    repository.replace_all(
        [
            BugRecord(
                bug_id=1227,
                product="内部钱包",
                module="未设置",
                title="测试标题",
                severity=2,
                priority=3,
                status="closed",
                bug_type="codeerror",
                reproduction_steps="",
                created_by="马丁",
                assigned_to="",
                created_at="2026-02-09T09:38:04Z",
                resolved_by="马丁",
                resolution="external",
                resolved_at="2026-03-03T10:35:54Z",
                closed_at="2026-03-03T10:35:58Z",
                is_reopened=False,
                synced_at="2026-07-28T00:00:00+00:00",
            )
        ]
    )
    app = create_app(database)

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/bugs/1227")
            assert response.status_code == 200
            data = response.json()
            assert data["title"] == "测试标题"
            assert data["severity_label"] == "严重 (2)"
            assert data["resolution_label"] == "外部原因 (external)"

            missing = await client.get("/bugs/9999")
            assert missing.status_code == 404

            search = await client.get("/bugs", params={"status": "closed"})
            assert search.status_code == 200
            assert search.json()["count"] == 1

    asyncio.run(exercise())
