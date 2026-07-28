from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .models import BugRecord


COLUMNS = (
    "bug_id",
    "product",
    "module",
    "title",
    "severity",
    "priority",
    "status",
    "bug_type",
    "reproduction_steps",
    "created_by",
    "assigned_to",
    "created_at",
    "resolved_by",
    "resolution",
    "resolved_at",
    "closed_at",
    "is_reopened",
    "synced_at",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS bugs (
    bug_id INTEGER PRIMARY KEY,
    product TEXT NOT NULL,
    module TEXT NOT NULL,
    title TEXT NOT NULL,
    severity INTEGER,
    priority INTEGER,
    status TEXT NOT NULL,
    bug_type TEXT NOT NULL,
    reproduction_steps TEXT NOT NULL,
    created_by TEXT NOT NULL,
    assigned_to TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_by TEXT NOT NULL,
    resolution TEXT NOT NULL,
    resolved_at TEXT NOT NULL,
    closed_at TEXT NOT NULL,
    is_reopened INTEGER NOT NULL CHECK (is_reopened IN (0, 1)),
    synced_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bugs_status ON bugs(status);
CREATE INDEX IF NOT EXISTS idx_bugs_severity ON bugs(severity);
CREATE INDEX IF NOT EXISTS idx_bugs_module ON bugs(module);
CREATE INDEX IF NOT EXISTS idx_bugs_resolution ON bugs(resolution);
"""


class BugRepository:
    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)

    def connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.database_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def replace_all(self, records: Iterable[BugRecord]) -> int:
        rows = list(records)
        placeholders = ", ".join("?" for _ in COLUMNS)
        insert_sql = f"INSERT INTO bugs ({', '.join(COLUMNS)}) VALUES ({placeholders})"
        values = [
            tuple(
                int(getattr(record, key)) if key == "is_reopened" else getattr(record, key)
                for key in COLUMNS
            )
            for record in rows
        ]

        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM bugs")
            conn.executemany(insert_sql, values)
        return len(rows)

    def get(self, bug_id: int) -> dict[str, Any] | None:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM bugs WHERE bug_id = ?", (bug_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def search(
        self,
        *,
        keyword: str = "",
        status: str = "",
        severity: int | None = None,
        module: str = "",
        resolution: str = "",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        self.initialize()
        clauses: list[str] = []
        params: list[Any] = []

        if keyword:
            pattern = f"%{keyword}%"
            clauses.append(
                "(title LIKE ? OR reproduction_steps LIKE ? OR created_by LIKE ? OR assigned_to LIKE ?)"
            )
            params.extend([pattern] * 4)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if severity is not None:
            clauses.append("severity = ?")
            params.append(severity)
        if module:
            clauses.append("module LIKE ?")
            params.append(f"%{module}%")
        if resolution:
            clauses.append("resolution = ?")
            params.append(resolution)

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(limit, 100)))
        sql = f"SELECT * FROM bugs{where} ORDER BY bug_id DESC LIMIT ?"
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS bug_count, MAX(synced_at) AS last_synced_at FROM bugs"
            ).fetchone()
        return {"bug_count": int(row["bug_count"]), "last_synced_at": row["last_synced_at"] or ""}

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["is_reopened"] = bool(result["is_reopened"])
        return result
