"""SQLite-backed persistence for /v1/check reports.

Kept separate from the rules index (store.py) so the audit log survives
re-indexing and can be inspected or shipped on its own. Pure stdlib sqlite3;
a lock serializes access because FastAPI runs sync endpoints (and threadpool
calls) on arbitrary worker threads — hence ``check_same_thread=False``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .models import CheckReport

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    status TEXT NOT NULL,
    breaking INTEGER NOT NULL,
    files INTEGER NOT NULL,
    lines INTEGER NOT NULL,
    elapsed REAL NOT NULL,
    report_json TEXT NOT NULL
);
"""

_SUMMARY_COLS = "id, created_at, status, breaking, files, lines, elapsed"


class ReportStore:
    """One row per /v1/check run — a queryable audit log of gate decisions."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> ReportStore:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def save(self, report: CheckReport) -> str:
        """Persist a report; returns the generated check id (uuid4 hex)."""
        check_id = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                f"INSERT INTO checks ({_SUMMARY_COLS}, report_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (check_id, time.time(), report.verdict.status,
                 int(report.verdict.breaking_changes_detected),
                 report.files_changed, report.lines_added,
                 report.elapsed_seconds, report.model_dump_json()),
            )
            self._conn.commit()
        return check_id

    def get(self, check_id: str) -> dict | None:
        """Return the full stored report with its ``id`` merged in, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT report_json FROM checks WHERE id = ?", (check_id,)
            ).fetchone()
        if row is None:
            return None
        return json.loads(row[0]) | {"id": check_id}

    def list(self, limit: int = 50, offset: int = 0) -> list[dict]:
        """Summary rows, newest first — never the report blob."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_SUMMARY_COLS} FROM checks"
                " ORDER BY rowid DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [
            {"id": rid, "created_at": created, "status": status,
             "breaking": bool(breaking), "files": files, "lines": lines,
             "elapsed": elapsed}
            for rid, created, status, breaking, files, lines, elapsed in rows
        ]

    def stats(self) -> dict:
        """Aggregate over every stored report: per-status counts and means."""
        with self._lock:
            by_status = dict(self._conn.execute(
                "SELECT status, COUNT(*) FROM checks GROUP BY status"
            ).fetchall())
            total, avg_elapsed = self._conn.execute(
                "SELECT COUNT(*), AVG(elapsed) FROM checks"
            ).fetchone()
        return {"total": total, "by_status": by_status,
                "avg_elapsed_seconds": avg_elapsed or 0.0}
