from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Path as ApiPath, Query
from pydantic import BaseModel, ConfigDict, Field

from .db import BugRepository
from .models import (
    RESOLUTION_LABELS,
    SEVERITY_LABELS,
    STATUS_LABELS,
    TYPE_LABELS,
    display_code,
    display_number,
)


DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "bugs.sqlite3"


class BugResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bug_id: int = Field(description="Bug 编号")
    product: str = Field(description="产品名称")
    module: str = Field(description="所属模块；未设置时明确返回“未设置”")
    title: str = Field(description="Bug 标题")
    severity: int | None = Field(description="禅道严重程度原始数字")
    severity_label: str = Field(description="严重程度中文说明")
    priority: int | None = Field(description="处理优先级")
    status: str = Field(description="禅道状态原始编码")
    status_label: str = Field(description="状态中文说明")
    bug_type: str = Field(description="Bug 类型原始编码")
    bug_type_label: str = Field(description="Bug 类型中文说明")
    reproduction_steps: str = Field(description="去除 HTML 后的重现步骤")
    created_by: str = Field(description="创建人")
    assigned_to: str = Field(description="当前负责人")
    created_at: str = Field(description="创建时间")
    resolved_by: str = Field(description="解决人")
    resolution: str = Field(description="解决方案原始编码")
    resolution_label: str = Field(description="解决方案中文说明")
    resolved_at: str = Field(description="解决时间")
    closed_at: str = Field(description="关闭时间")
    is_reopened: bool = Field(description="是否曾重新激活")
    synced_at: str = Field(description="本地数据同步时间")


class BugSearchResponse(BaseModel):
    count: int
    bugs: list[BugResponse]


class HealthResponse(BaseModel):
    status: str
    bug_count: int
    last_synced_at: str


def present_bug(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "severity_label": display_number(row["severity"], SEVERITY_LABELS),
        "status_label": display_code(str(row["status"]), STATUS_LABELS),
        "bug_type_label": display_code(str(row["bug_type"]), TYPE_LABELS),
        "resolution_label": display_code(str(row["resolution"]), RESOLUTION_LABELS),
    }


def create_app(database_path: str | Path | None = None) -> FastAPI:
    configured_path = database_path or os.getenv("BUG_DB_PATH") or DEFAULT_DB_PATH
    repository = BugRepository(configured_path)
    repository.initialize()

    app = FastAPI(
        title="禅道 Bug 精确查询工具",
        description=(
            "供 Open WebUI 调用的只读工具。查询明确的 Bug 编号时，必须优先调用 "
            "get_bug_by_id，不要使用知识库向量检索猜测字段。"
        ),
        version="1.0.0",
    )

    @app.get(
        "/health",
        response_model=HealthResponse,
        operation_id="get_bug_service_health",
        summary="检查 Bug 查询服务及数据状态",
    )
    def health() -> dict[str, Any]:
        return {"status": "ok", **repository.stats()}

    @app.get(
        "/bugs/{bug_id}",
        response_model=BugResponse,
        operation_id="get_bug_by_id",
        summary="按 Bug 编号精确查询完整字段",
        description="适用于“查询 Bug 1227”一类问题。未找到时返回 HTTP 404。",
    )
    def get_bug(
        bug_id: Annotated[int, ApiPath(ge=1, description="禅道 Bug 编号")],
    ) -> dict[str, Any]:
        row = repository.get(bug_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"Bug {bug_id} 不存在")
        return present_bug(row)

    @app.get(
        "/bugs",
        response_model=BugSearchResponse,
        operation_id="search_bugs",
        summary="按结构化条件筛选 Bug",
        description="可组合关键词、状态、严重程度、模块和解决方案筛选，最多返回 100 条。",
    )
    def search_bugs(
        keyword: Annotated[str, Query(description="标题、步骤、创建人或负责人关键词")] = "",
        status: Annotated[str, Query(description="状态原始编码，如 active、resolved、closed")] = "",
        severity: Annotated[int | None, Query(ge=1, le=4, description="严重程度 1-4")] = None,
        module: Annotated[str, Query(description="模块名称关键词")] = "",
        resolution: Annotated[str, Query(description="解决方案原始编码，如 fixed、external")] = "",
        limit: Annotated[int, Query(ge=1, le=100, description="最大返回条数")] = 20,
    ) -> dict[str, Any]:
        rows = repository.search(
            keyword=keyword.strip(),
            status=status.strip(),
            severity=severity,
            module=module.strip(),
            resolution=resolution.strip(),
            limit=limit,
        )
        return {"count": len(rows), "bugs": [present_bug(row) for row in rows]}

    return app


app = create_app()
