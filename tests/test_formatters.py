"""Tests for the enterprise renderers: json/markdown/sarif/junit."""

import json
import xml.etree.ElementTree as ET

import pytest
from typer.testing import CliRunner

from gitport import __version__
from gitport.formatters import render
from gitport.models import CheckReport, Flaw, ToolCallRecord, Verdict

runner = CliRunner()

FLAWS = [
    Flaw(file="migrations/0012.sql", line=3, issue="DELETE without WHERE",
         fix_suggestion="add a WHERE clause", severity="critical"),
    Flaw(file="app/util.py", line=12, issue="eval() on user input", severity="medium"),
    Flaw(file="app/log.py", line=0, issue="noisy logging", severity="low"),
]


def _report(status="FAILED", flaws=None, **kw):
    return CheckReport(
        verdict=Verdict(
            status=status,
            breaking_changes_detected=kw.pop("breaking", status == "FAILED"),
            critical_flaws=list(flaws if flaws is not None else FLAWS),
            summary=kw.pop("summary", "found problems"),
            rules_applied=kw.pop("rules", ["rules/auth.md"]),
        ),
        tool_calls=[ToolCallRecord(name="lint_sql", ok=True, summary="1 issue")],
        agent_notes="agent looked at migrations",
        files_changed=2, lines_added=10, elapsed_seconds=1.25,
        **kw,
    )


def test_render_json_round_trips():
    out = render(_report(), "json")
    data = json.loads(out)
    assert data["verdict"]["status"] == "FAILED"
    assert data["verdict"]["critical_flaws"][0]["file"] == "migrations/0012.sql"
    assert "\n" in out  # indent=2


def test_render_unknown_format_raises():
    with pytest.raises(ValueError, match="unknown format"):
        render(_report(), "xml")


def test_markdown_has_verdict_table_details_footer():
    out = render(_report(), "markdown")
    assert "gitport: FAILED" in out
    assert "❌" in out
    assert "found problems" in out
    assert "| file | line | severity | issue | fix |" in out
    assert "| migrations/0012.sql | 3 | critical | DELETE without WHERE" in out
    assert "- rules/auth.md" in out
    assert "<details>" in out and "</details>" in out
    assert "agent looked at migrations" in out
    assert "| lint_sql | ✅ | 1 issue |" in out
    assert f"gitport {__version__} — 1.2s" in out


def test_markdown_escapes_pipes_and_newlines():
    flaw = Flaw(file="a.py", line=1, issue="x | y\nz", severity="low")
    out = render(_report(status="PASSED", flaws=[flaw], breaking=False), "markdown")
    assert "x \\| y z" in out
    # the escaped pipe must not split into an extra cell
    row = next(line for line in out.splitlines() if line.startswith("| a.py"))
    assert row.replace("\\|", "").count("|") == 6  # leading + trailing + 4 separators


def test_sarif_document_shape_and_levels():
    doc = json.loads(render(_report(), "sarif"))
    assert doc["version"] == "2.1.0"
    run = doc["runs"][0]
    driver = run["tool"]["driver"]
    assert driver["name"] == "gitport"
    assert driver["version"] == __version__
    assert [r["id"] for r in driver["rules"]] == [
        "delete-without-where", "eval-on-user-input", "noisy-logging"]

    results = run["results"]
    assert len(results) == 3
    assert [r["level"] for r in results] == ["error", "warning", "note"]
    loc = results[0]["locations"][0]["physicalLocation"]
    assert loc["artifactLocation"]["uri"] == "migrations/0012.sql"
    assert loc["region"]["startLine"] == 3
    # line=0 clamps to 1 — SARIF regions are 1-based
    assert results[2]["locations"][0]["physicalLocation"]["region"]["startLine"] == 1
    assert "suggested fix" in results[0]["message"]["text"]


def test_sarif_dedupes_rules_and_handles_empty():
    dup = FLAWS[0].model_copy(update={"line": 9})
    doc = json.loads(render(_report(flaws=[FLAWS[0], dup]), "sarif"))
    run = doc["runs"][0]
    assert len(run["tool"]["driver"]["rules"]) == 1
    assert len(run["results"]) == 2

    clean = json.loads(render(_report(status="PASSED", flaws=[], breaking=False), "sarif"))
    assert clean["runs"][0]["results"] == []
    assert clean["runs"][0]["tool"]["driver"]["rules"] == []


def test_junit_structure_and_failures():
    root = ET.fromstring(render(_report(), "junit"))
    assert root.tag == "testsuite" and root.get("name") == "gitport"
    # 3 flaws + the gitport-verdict testcase
    assert root.get("tests") == "4"
    # critical flaw fails, medium/low don't; FAILED verdict adds one more
    assert root.get("failures") == "2"

    cases = root.findall("testcase")
    assert cases[0].get("classname") == "migrations/0012.sql"
    assert cases[0].get("name") == "DELETE without WHERE"
    assert cases[0].find("failure") is not None
    assert cases[1].find("failure") is None  # medium passes
    verdict = cases[-1]
    assert verdict.get("name") == "gitport-verdict"
    assert verdict.find("failure") is not None


def test_junit_name_truncated_and_escaped():
    flaw = Flaw(file='a"b.py', line=1, issue="<" * 200, severity="high")
    root = ET.fromstring(render(_report(flaws=[flaw]), "junit"))
    case = root.findall("testcase")[0]
    assert case.get("classname") == 'a"b.py'
    assert len(case.get("name")) == 80


def test_junit_passed_verdict_no_failure():
    root = ET.fromstring(
        render(_report(status="PASSED", flaws=[], breaking=False), "junit"))
    assert root.get("failures") == "0"
    verdict = root.findall("testcase")[-1]
    assert verdict.get("name") == "gitport-verdict"
    assert verdict.find("failure") is None


def test_cli_format_flags_on_empty_diff(tmp_path):
    """--format works end-to-end; --json stays a working alias."""
    from gitport.cli import app

    f = tmp_path / "empty.diff"
    f.write_text("")

    res = runner.invoke(app, ["check", "--diff-file", str(f), "--format", "sarif"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["version"] == "2.1.0"

    res = runner.invoke(app, ["check", "--diff-file", str(f), "--format", "markdown"])
    assert res.exit_code == 0
    assert "gitport: PASSED" in res.output

    res = runner.invoke(app, ["check", "--diff-file", str(f), "--format", "junit"])
    assert res.exit_code == 0
    assert ET.fromstring(res.output).get("name") == "gitport"

    res = runner.invoke(app, ["check", "--diff-file", str(f), "--json"])
    assert res.exit_code == 0
    assert json.loads(res.output)["verdict"]["status"] == "PASSED"


def test_cli_format_rejects_unknown(tmp_path):
    from gitport.cli import app

    f = tmp_path / "empty.diff"
    f.write_text("")
    res = runner.invoke(app, ["check", "--diff-file", str(f), "--format", "yaml"])
    assert res.exit_code != 0
