"""Tests for the policy-as-code engine: load, apply, should_fail."""

import pytest
from typer.testing import CliRunner

from gitport.models import CheckReport, Flaw, Verdict
from gitport.policy import (
    Policy,
    PolicyError,
    apply_policy,
    load_policy,
    should_fail,
)

runner = CliRunner()


def _verdict(status="WARNING", breaking=False, flaws=None, summary=""):
    return Verdict(
        status=status,
        breaking_changes_detected=breaking,
        critical_flaws=list(flaws or []),
        summary=summary,
    )


FLAWS = [
    Flaw(file="vendor/lib.py", line=1, issue="eval() on input", severity="critical"),
    Flaw(file="app/util.py", line=5, issue="broad except", severity="medium"),
    Flaw(file="migrations/01.sql", line=2, issue="missing index", severity="low"),
]


def _write(tmp_path, text):
    p = tmp_path / "policy.toml"
    p.write_text(text)
    return p


# -- load_policy -------------------------------------------------------------


def test_load_policy_defaults_for_none_and_missing(tmp_path):
    for pol in (load_policy(None), load_policy(tmp_path / "absent.toml")):
        assert pol.severity_floor == "low"
        assert pol.fail_on == ["FAILED"]
        assert pol.suppress == [] and pol.escalate == []


def test_load_policy_parses_full_schema(tmp_path):
    p = _write(tmp_path, """\
severity_floor = "medium"
fail_on = ["FAILED", "WARNING"]

[[suppress]]
glob = "vendor/**"
match = "eval"
reason = "vendored code"

[[escalate]]
glob = "migrations/**"
""")
    pol = load_policy(p)
    assert pol.severity_floor == "medium"
    assert pol.fail_on == ["FAILED", "WARNING"]
    assert pol.suppress[0].glob == "vendor/**"
    assert pol.suppress[0].match == "eval"
    assert pol.suppress[0].reason == "vendored code"
    assert pol.escalate[0].glob == "migrations/**"


def test_load_policy_malformed_toml_raises(tmp_path):
    p = _write(tmp_path, "severity_floor = [oops")
    with pytest.raises(PolicyError, match="invalid TOML"):
        load_policy(p)


def test_load_policy_unknown_key_and_bad_value_raise(tmp_path):
    with pytest.raises(PolicyError, match="invalid policy"):
        load_policy(_write(tmp_path, 'surprise = "yes"'))
    with pytest.raises(PolicyError, match="invalid policy"):
        load_policy(_write(tmp_path, 'severity_floor = "catastrophic"'))


# -- apply_policy ------------------------------------------------------------


def test_apply_suppresses_by_glob_and_counts():
    pol = Policy(suppress=[{"glob": "vendor/**", "reason": "vendored"}])
    out = apply_policy(_verdict(flaws=FLAWS, summary="bad"), pol)
    assert [f.file for f in out.critical_flaws] == ["app/util.py", "migrations/01.sql"]
    assert "1 finding suppressed by policy" in out.summary
    assert "vendored" in out.summary


def test_apply_suppress_match_substring():
    pol = Policy(suppress=[{"glob": "**", "match": "broad"}])
    out = apply_policy(_verdict(flaws=FLAWS), pol)
    # only "broad except" matches the substring, case-insensitively
    assert [f.issue for f in out.critical_flaws] == [
        "eval() on input", "missing index"]
    assert "1 finding suppressed by policy" in out.summary


def test_apply_severity_floor_drops_lower():
    pol = Policy(severity_floor="medium")
    out = apply_policy(_verdict(flaws=FLAWS), pol)
    assert [f.file for f in out.critical_flaws] == ["vendor/lib.py", "app/util.py"]
    assert "suppressed" not in out.summary  # floor drops aren't suppressions


def test_apply_escalates_to_high_and_fails():
    pol = Policy(escalate=[{"glob": "migrations/**"}])
    out = apply_policy(
        _verdict(flaws=[Flaw(file="migrations/01.sql", line=2,
                             issue="missing index", severity="low")]),
        pol,
    )
    assert out.critical_flaws[0].severity == "high"
    assert out.status == "FAILED"


def test_escalate_never_downgrades_critical():
    pol = Policy(escalate=[{"glob": "**"}])
    out = apply_policy(_verdict(flaws=[FLAWS[0]]), pol)
    assert out.critical_flaws[0].severity == "critical"


def test_apply_recomputes_status():
    # everything suppressed, nothing breaking → PASSED
    pol = Policy(suppress=[{"glob": "**"}])
    out = apply_policy(_verdict(status="WARNING", flaws=FLAWS), pol)
    assert out.status == "PASSED"
    assert "3 findings suppressed by policy" in out.summary

    # breaking changes still fail even with an empty flaw list
    out = apply_policy(_verdict(status="WARNING", breaking=True), Policy())
    assert out.status == "FAILED"

    # remaining medium/low flaws → WARNING
    out = apply_policy(
        _verdict(flaws=[Flaw(file="a.py", issue="nits", severity="low")]), Policy())
    assert out.status == "WARNING"

    # an already-FAILED verdict stays FAILED even if policy cleans the slate
    out = apply_policy(
        _verdict(status="FAILED", flaws=FLAWS),
        Policy(suppress=[{"glob": "**"}]),
    )
    assert out.status == "FAILED"


def test_apply_returns_new_verdict():
    v = _verdict(flaws=FLAWS)
    out = apply_policy(v, Policy(suppress=[{"glob": "vendor/**"}]))
    assert out is not v
    assert len(v.critical_flaws) == 3  # untouched


def test_should_fail_uses_fail_on():
    v = _verdict(status="WARNING")
    assert should_fail(v, Policy()) is False
    assert should_fail(v, Policy(fail_on=["FAILED", "WARNING"])) is True
    assert should_fail(_verdict(status="FAILED"), Policy()) is True


# -- CLI wiring ---------------------------------------------------------------


def _out(res) -> str:
    """stdout + stderr across click/typer CliRunner versions."""
    try:
        return res.output + (res.stderr or "")
    except Exception:
        return res.output


def test_cli_policy_file_changes_exit_code(tmp_path, monkeypatch):
    """Escalation turns a WARNING check (exit 0) into a FAILED one (exit 1)."""
    import gitport.cli as cli

    report = CheckReport(verdict=Verdict(
        status="WARNING", breaking_changes_detected=False, summary="risky",
        critical_flaws=[Flaw(file="migrations/x.sql", line=1,
                             issue="missing index", severity="low")]))

    # The real run_check applies the policy inside the engine; the mock
    # mirrors that contract so the CLI's load-and-pass wiring is exercised.
    def _fake_run_check(*a, policy=None, **k):
        v = apply_policy(report.verdict, policy) if policy else report.verdict
        return report.model_copy(update={"verdict": v})

    monkeypatch.setattr(cli, "run_check", _fake_run_check)

    diff = tmp_path / "d.diff"
    diff.write_text("diff")
    pol = _write(tmp_path, '[[escalate]]\nglob = "migrations/**"\n')

    res = runner.invoke(cli.app, ["check", "--diff-file", str(diff)])
    assert res.exit_code == 0, _out(res)  # plain WARNING passes the gate
    res = runner.invoke(cli.app, ["check", "--diff-file", str(diff),
                                  "--policy", str(pol)])
    assert res.exit_code == 1, _out(res)
    assert "FAILED" in _out(res)


def test_cli_bad_policy_exits_3(tmp_path):
    import gitport.cli as cli

    diff = tmp_path / "d.diff"
    diff.write_text("")
    pol = _write(tmp_path, 'severity_floor = "bogus"')
    res = runner.invoke(cli.app, ["check", "--diff-file", str(diff),
                                  "--policy", str(pol)])
    assert res.exit_code == 3
    assert "invalid policy file" in _out(res)
