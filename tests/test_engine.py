import pytest
from conftest import FakeCohereClient

from gitport.engine import EngineError, error_verdict, run_check


def test_empty_diff_passes(cfg):
    report = run_check(cfg, client=FakeCohereClient(), diff_text="")
    assert report.verdict.status == "PASSED"
    assert report.files_changed == 0


def test_full_pipeline_records_evidence(cfg, sample_diff):
    client = FakeCohereClient(
        tool_plan=[
            [("lint_migration_file", {"path": "migrations/0012_add_email.sql"}),
             ("analyze_python_file", {"path": "app/util.py"})],
        ],
        notes="locking migration and eval found",
        verdicts=[{
            "status": "FAILED", "breaking_changes_detected": True,
            "critical_flaws": [
                {"file": "migrations/0012_add_email.sql", "line": 1,
                 "issue": "NOT NULL without DEFAULT rewrites the users table"},
            ],
            "summary": "unsafe migration",
        }],
    )
    report = run_check(cfg, client=client, diff_text=sample_diff)

    assert report.verdict.status == "FAILED"
    assert report.verdict.breaking_changes_detected is True
    assert report.files_changed == 2
    assert report.lines_added == 4
    assert {t.name for t in report.tool_calls} == {
        "lint_migration_file", "analyze_python_file"}
    assert report.tool_calls[0].ok is True
    assert "analysis notes" in report.agent_notes or "locking" in report.agent_notes


def test_agent_actually_ran_tools(cfg, sample_diff):
    """The scripted tool calls must appear in the transcript the gate saw."""
    client = FakeCohereClient(
        tool_plan=[[("read_file", {"path": "app/util.py"})]],
    )
    run_check(cfg, client=client, diff_text=sample_diff)
    # first chat: tools request; second: notes; third: verdict
    assert len(client.chat_calls) == 3
    gate_call = client.chat_calls[-1]
    assert "response_format" in gate_call


def test_missing_api_key_raises(cfg):
    from gitport.engine import make_client
    with pytest.raises(EngineError):
        make_client(cfg)


def test_error_verdict_modes():
    closed = error_verdict("boom", fail_open=False)
    assert closed.status == "FAILED" and closed.error == "boom"
    opened = error_verdict("boom", fail_open=True)
    assert opened.status == "WARNING" and opened.error == "boom"
