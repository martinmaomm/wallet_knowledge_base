from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


STATUS_LABELS = {
    "active": "激活",
    "resolved": "已解决",
    "closed": "已关闭",
}

SEVERITY_LABELS = {
    1: "致命",
    2: "严重",
    3: "一般",
    4: "轻微",
}

TYPE_LABELS = {
    "codeerror": "代码错误",
    "config": "配置相关",
    "install": "安装部署",
    "security": "安全相关",
    "performance": "性能问题",
    "standard": "标准规范",
    "automation": "测试脚本",
    "designdefect": "设计缺陷",
    "others": "其他",
}

RESOLUTION_LABELS = {
    "fixed": "已修复",
    "duplicate": "重复 Bug",
    "external": "外部原因",
    "postponed": "延期处理",
    "willnotfix": "不予修复",
    "notrepro": "无法重现",
    "bydesign": "设计如此",
    "tostory": "转为需求",
}


def display_code(code: str, labels: dict[str, str]) -> str:
    if not code:
        return ""
    label = labels.get(code)
    return f"{label} ({code})" if label else code


def display_number(value: int | None, labels: dict[int, str]) -> str:
    if value is None:
        return ""
    label = labels.get(value)
    return f"{label} ({value})" if label else str(value)


@dataclass(frozen=True)
class BugRecord:
    bug_id: int
    product: str
    module: str
    title: str
    severity: int | None
    priority: int | None
    status: str
    bug_type: str
    reproduction_steps: str
    created_by: str
    assigned_to: str
    created_at: str
    resolved_by: str
    resolution: str
    resolved_at: str
    closed_at: str
    is_reopened: bool
    synced_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
