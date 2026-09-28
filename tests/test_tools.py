
from gitport.diff import parse_unified_diff
from gitport.tools import ToolContext, execute_tool, summarize_result


def _ctx(sample_diff, repo_root=None):
    return ToolContext(
        file_diffs={f.path: f for f in parse_unified_diff(sample_diff)},
        repo_root=repo_root,
    )


def test_unknown_tool():
    res = execute_tool(ToolContext(), "nope", {})
    assert res["ok"] is False
    assert "unknown tool" in res["error"]


def test_lint_migration_from_diff_only(sample_diff):
    res = execute_tool(_ctx(sample_diff), "lint_migration_file",
                       {"path": "migrations/0012_add_email.sql"})
    assert res["ok"] is True
    assert res["source"] == "diff"
    assert res["hazardous"] is True


def test_analyze_python_from_diff_only(sample_diff):
    res = execute_tool(_ctx(sample_diff), "analyze_python_file",
                       {"path": "app/util.py"})
    assert res["ok"] is True
    assert any(d["call"] == "eval" for d in res["dangerous_calls"])


def test_analyze_rejects_non_python(sample_diff):
    res = execute_tool(_ctx(sample_diff), "analyze_python_file",
                       {"path": "migrations/0012_add_email.sql"})
    assert res["ok"] is False


def test_read_file_sandbox_blocks_escape(tmp_path, sample_diff):
    (tmp_path / "secret.txt").write_text("top secret")
    ctx = _ctx(sample_diff, repo_root=tmp_path)
    res = execute_tool(ctx, "read_file", {"path": "../outside.txt"})
    assert res["ok"] is False
    res = execute_tool(ctx, "read_file", {"path": "secret.txt"})
    assert res["ok"] is True
    assert res["content"] == "top secret"


def test_missing_file_returns_error(sample_diff):
    res = execute_tool(_ctx(sample_diff), "read_file", {"path": "ghost.py"})
    assert res["ok"] is False


def test_summarize_result():
    assert summarize_result({"ok": False, "error": "boom"}).startswith("error")
    assert "2 findings" in summarize_result({"ok": True, "findings": [1, 2]})
