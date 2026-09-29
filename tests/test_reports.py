"""ReportStore: save/get round-trips, summaries, stats, thread safety."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from gitport.models import CheckReport, Verdict
from gitport.reports import ReportStore


def _report(status="PASSED", breaking=False, elapsed=0.5) -> CheckReport:
    return CheckReport(
        verdict=Verdict(status=status, breaking_changes_detected=breaking,
                        summary="s"),
        files_changed=2,
        lines_added=5,
        elapsed_seconds=elapsed,
    )


def test_save_and_get(tmp_path):
    with ReportStore(tmp_path / "reports.sqlite3") as store:
        check_id = store.save(_report("FAILED", breaking=True))
        row = store.get(check_id)
        missing = store.get("does-not-exist")
    assert missing is None
    assert row["id"] == check_id
    assert row["verdict"]["status"] == "FAILED"
    assert row["verdict"]["breaking_changes_detected"] is True
    assert row["files_changed"] == 2
    assert row["lines_added"] == 5

    # the stored blob is the raw report — the id lives in its own column
    conn = sqlite3.connect(str(tmp_path / "reports.sqlite3"))
    stored = json.loads(conn.execute(
        "SELECT report_json FROM checks WHERE id = ?", (check_id,)
    ).fetchone()[0])
    conn.close()
    assert stored["verdict"]["status"] == "FAILED"
    assert "id" not in stored


def test_list_summaries_newest_first(tmp_path):
    with ReportStore(tmp_path / "r.db") as store:
        ids = [store.save(_report()) for _ in range(3)]
        page = store.list(limit=2)
        rest = store.list(limit=2, offset=2)
    assert [r["id"] for r in page] == ids[::-1][:2]
    assert [r["id"] for r in rest] == [ids[0]]
    summary = page[0]
    assert set(summary) == {"id", "created_at", "status", "breaking",
                            "files", "lines", "elapsed"}
    assert summary["status"] == "PASSED"
    assert summary["breaking"] is False


def test_stats(tmp_path):
    with ReportStore(tmp_path / "r.db") as store:
        store.save(_report("PASSED", elapsed=1.0))
        store.save(_report("PASSED", elapsed=3.0))
        store.save(_report("FAILED", breaking=True, elapsed=2.0))
        stats = store.stats()
    assert stats["total"] == 3
    assert stats["by_status"] == {"PASSED": 2, "FAILED": 1}
    assert stats["avg_elapsed_seconds"] == pytest.approx(2.0)


def test_stats_empty(tmp_path):
    with ReportStore(tmp_path / "r.db") as store:
        stats = store.stats()
    assert stats == {"total": 0, "by_status": {}, "avg_elapsed_seconds": 0.0}


def test_concurrent_saves(tmp_path):
    # the API runs store calls on a threadpool — writes must serialize
    with ReportStore(tmp_path / "r.db") as store:
        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(lambda _: store.save(_report()), range(16)))
        total = store.stats()["total"]
    assert len(set(ids)) == 16
    assert total == 16


def test_creates_parent_dirs(tmp_path):
    db = tmp_path / "nested" / "deep" / "r.db"
    with ReportStore(db) as store:
        assert store.save(_report())
    assert db.exists()
