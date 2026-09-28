
from gitport.analysis import (
    analyze_python_source,
    detect_manifest,
    lint_sql_migration,
    parse_pinned_deps,
    scan_dependencies,
)


def test_lint_flags_locking_migration():
    sql = """\
ALTER TABLE users ADD COLUMN email varchar(255) NOT NULL;
CREATE INDEX idx_users_email ON users(email);
DELETE FROM sessions;
"""
    result = lint_sql_migration(sql)
    rules = {f["rule"] for f in result["findings"]}
    assert result["hazardous"] is True
    assert "NOT_NULL_NO_DEFAULT" in rules
    assert "INDEX_NO_CONCURRENTLY" in rules
    assert "DELETE_NO_WHERE" in rules


def test_lint_passes_safe_migration():
    sql = """\
ALTER TABLE users ADD COLUMN email varchar(255);
CREATE INDEX CONCURRENTLY idx_users_email ON users(email);
UPDATE users SET email = '' WHERE email IS NULL;
"""
    result = lint_sql_migration(sql)
    assert result["hazardous"] is False
    assert result["findings"] == []


def test_ast_analysis_catches_eval():
    res = analyze_python_source('def f():\n    return eval("1+1")\n')
    assert res["ok"] is True
    assert res["functions"][0]["name"] == "f"
    assert any(d["call"] == "eval" for d in res["dangerous_calls"])


def test_ast_analysis_catches_shell_true():
    src = "import subprocess\nsubprocess.run(cmd, shell=True)\n"
    res = analyze_python_source(src)
    assert any("shell=True" in d["call"] for d in res["dangerous_calls"])
    res2 = analyze_python_source("import subprocess\nsubprocess.run(['ls'])\n")
    assert res2["dangerous_calls"] == []


def test_ast_analysis_syntax_error():
    res = analyze_python_source("def broken(:\n")
    assert res["ok"] is False
    assert "syntax_error" in res


def test_parse_pinned_deps():
    text = "cohere==5.16.0\n# comment\nhttpx>=0.27\n-r other.txt\n"
    assert parse_pinned_deps(text) == [("cohere", "5.16.0"), ("httpx", "0.27")]


def test_detect_manifest():
    assert detect_manifest("requirements.txt") == "PyPI"
    assert detect_manifest("sub/dir/package.json") == "npm"
    assert detect_manifest("app/util.py") is None


class _FakeHTTP:
    def post(self, url, json):
        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"results": [
                    {"vulns": [{"id": "CVE-1", "summary": "bad",
                                "severity": [{"score": "9.8"}]}]},
                    {},
                ]}
        return R()


def test_scan_dependencies_reports_vulns():
    res = scan_dependencies("flask==2.0.0\nrequests==2.31.0\n", http=_FakeHTTP())
    assert res["ok"] is True
    assert res["packages"] == 2
    assert res["vulnerabilities"][0]["package"] == "flask"
    assert res["vulnerabilities"][0]["id"] == "CVE-1"


def test_scan_dependencies_network_failure_is_data():
    class Down:
        def post(self, *a, **k):
            raise ConnectionError("offline")

    res = scan_dependencies("flask==2.0.0\n", http=Down())
    assert res["ok"] is False
    assert "error" in res
