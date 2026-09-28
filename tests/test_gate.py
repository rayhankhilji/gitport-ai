import json

import pytest
from conftest import FakeCohereClient

from gitport.agent import AgentResult
from gitport.diff import parse_unified_diff
from gitport.gate import GateError, _parse_verdict, evaluate


def _agent():
    return AgentResult(notes="found a locking migration",
                       records=[], transcript=[{"tool": "lint", "result": {"ok": True}}])


def test_parse_plain_json():
    v = _parse_verdict(json.dumps({
        "status": "FAILED", "breaking_changes_detected": True,
        "critical_flaws": [{"file": "a.sql", "line": 1, "issue": "locks table"}],
    }))
    assert v.status == "FAILED"
    assert v.critical_flaws[0].file == "a.sql"


def test_parse_fenced_json():
    v = _parse_verdict('```json\n{"status": "PASSED", '
                       '"breaking_changes_detected": false, "critical_flaws": []}\n```')
    assert v.status == "PASSED"


def test_parse_garbage_raises():
    with pytest.raises(GateError):
        _parse_verdict("not json at all")


def test_parse_schema_violation_raises():
    with pytest.raises(GateError):
        _parse_verdict('{"status": "MAYBE", "breaking_changes_detected": false}')


def test_evaluate_happy_path(cfg, sample_diff):
    client = FakeCohereClient(verdicts=[{
        "status": "FAILED", "breaking_changes_detected": True,
        "critical_flaws": [{"file": "migrations/0012_add_email.sql", "line": 1,
                            "issue": "NOT NULL without DEFAULT locks the table",
                            "fix_suggestion": "add column nullable, backfill",
                            "severity": "high"}],
        "summary": "migration will lock users table",
        "rules_applied": ["migrations.md"],
    }])
    files = parse_unified_diff(sample_diff)
    v = evaluate(client, cfg, files, rules=[], agent=_agent())
    assert v.status == "FAILED"
    assert v.breaking_changes_detected is True
    # one schema-constrained call, no repair needed
    assert len(client.chat_calls) == 1
    assert client.chat_calls[0]["response_format"]["schema"]["required"] == [
        "status", "breaking_changes_detected", "critical_flaws"]


def test_evaluate_repairs_once(cfg, sample_diff):
    client = FakeCohereClient(verdicts=[
        "i think it is bad but here is prose",
        {"status": "WARNING", "breaking_changes_detected": False,
         "critical_flaws": [], "summary": "repaired"},
    ])
    files = parse_unified_diff(sample_diff)
    v = evaluate(client, cfg, files, rules=[], agent=_agent())
    assert v.status == "WARNING"
    assert len(client.chat_calls) == 2


def test_evaluate_double_failure_raises(cfg, sample_diff):
    client = FakeCohereClient(verdicts=["junk", "still junk"])
    files = parse_unified_diff(sample_diff)
    with pytest.raises(GateError):
        evaluate(client, cfg, files, rules=[], agent=_agent())
