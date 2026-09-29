from typer.testing import CliRunner

from gitport.cli import app

runner = CliRunner()


def test_check_empty_diff_file_passes(tmp_path):
    """No API key needed — an empty diff short-circuits to PASSED."""
    f = tmp_path / "empty.diff"
    f.write_text("")
    res = runner.invoke(app, ["check", "--diff-file", str(f)])
    assert res.exit_code == 0, res.output
    assert "PASSED" in res.output


def test_check_failed_exits_1(tmp_path, sample_diff, monkeypatch):
    from conftest import FakeCohereClient

    from gitport import engine

    monkeypatch.setattr(
        engine, "make_client",
        lambda cfg: FakeCohereClient(verdicts=[{
            "status": "FAILED", "breaking_changes_detected": True,
            "critical_flaws": [{"file": "m.sql", "line": 1, "issue": "locks"}],
        }]),
    )
    # cli imports run_check directly; engine.run_check keeps its own ref
    import gitport.cli as cli
    monkeypatch.setattr(cli, "run_check", engine.run_check)

    f = tmp_path / "bad.diff"
    f.write_text(sample_diff)
    res = runner.invoke(app, ["check", "--diff-file", str(f)])
    assert res.exit_code == 1
    assert "FAILED" in res.output


def test_json_output(tmp_path, sample_diff, monkeypatch):
    from conftest import FakeCohereClient

    import gitport.cli as cli
    from gitport import engine

    monkeypatch.setattr(engine, "make_client", lambda cfg: FakeCohereClient())
    monkeypatch.setattr(cli, "run_check", engine.run_check)

    f = tmp_path / "d.diff"
    f.write_text(sample_diff)
    res = runner.invoke(app, ["check", "--diff-file", str(f), "--json"])
    assert res.exit_code == 0
    assert '"status"' in res.output


def test_version():
    res = runner.invoke(app, ["version"])
    assert res.exit_code == 0
    assert "gitport" in res.output


def test_install_hook_bare_repo(tmp_path):
    import subprocess
    bare = tmp_path / "srv.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    res = runner.invoke(app, ["install-hook", "pre-receive", "--repo", str(bare)])
    assert res.exit_code == 0
    hook = bare / "hooks" / "pre-receive"
    assert hook.exists() and hook.stat().st_mode & 0o111
    assert "gitport check" in hook.read_text()


def test_install_hook_regular_repo(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    res = runner.invoke(app, ["install-hook", "--repo", str(tmp_path)])
    assert res.exit_code == 0
    assert (tmp_path / ".git" / "hooks" / "pre-push").exists()
