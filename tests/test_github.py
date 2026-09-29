"""Tests for the GitHub client: PR comment upserts and commit statuses."""

import pytest
from typer.testing import CliRunner

from gitport import __version__
from gitport.github import GithubError, post_pr_comment, set_commit_status
from gitport.models import CheckReport, Verdict

runner = CliRunner()

TOKEN = "ghp_test123"


def _report(status="FAILED"):
    return CheckReport(verdict=Verdict(
        status=status, breaking_changes_detected=(status == "FAILED"),
        summary="looks risky"))


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class FakeHttp:
    """Stand-in for httpx.Client: records calls, replays canned responses."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.responses: dict[str, list[FakeResponse]] = {}

    def queue(self, method, *responses):
        self.responses[method] = list(responses)

    def _do(self, method, url, **kw):
        self.calls.append((method, url, kw))
        queued = self.responses.get(method)
        if queued:
            return queued.pop(0)
        return FakeResponse(200, {})

    def get(self, url, **kw):
        return self._do("GET", url, **kw)

    def post(self, url, **kw):
        return self._do("POST", url, **kw)

    def patch(self, url, **kw):
        return self._do("PATCH", url, **kw)


def _headers_of(call):
    return call[2]["headers"]


# -- post_pr_comment -----------------------------------------------------------


def test_comment_created_when_no_marker():
    http = FakeHttp()
    http.queue("GET", FakeResponse(200, [{"id": 1, "body": "lgtm"}]))
    http.queue("POST", FakeResponse(201, {"id": 9, "body": "<!-- gitport -->"}))

    out = post_pr_comment(_report(), "acme/app", 7, token=TOKEN, http=http)

    assert out["action"] == "created"
    assert out["comment"]["id"] == 9
    methods = [c[0] for c in http.calls]
    assert methods == ["GET", "POST"]
    url = http.calls[1][1]
    assert url == "https://api.github.com/repos/acme/app/issues/7/comments"
    body = http.calls[1][2]["json"]["body"]
    assert body.startswith("<!-- gitport -->")
    assert "gitport: FAILED" in body  # markdown report embedded


def test_comment_updates_existing_marker():
    http = FakeHttp()
    http.queue("GET", FakeResponse(200, [
        {"id": 1, "body": "lgtm"},
        {"id": 42, "body": "<!-- gitport -->\nold report"},
    ]))
    http.queue("PATCH", FakeResponse(200, {"id": 42}))

    out = post_pr_comment(_report(), "acme/app", 7, token=TOKEN, http=http)

    assert out["action"] == "updated"
    methods = [c[0] for c in http.calls]
    assert methods == ["GET", "PATCH"]
    assert http.calls[1][1].endswith("/repos/acme/app/issues/comments/42")
    assert "<!-- gitport -->" in http.calls[1][2]["json"]["body"]


def test_comment_headers_and_env_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "env_token")
    http = FakeHttp()
    http.queue("GET", FakeResponse(200, []))

    post_pr_comment(_report("PASSED"), "a/b", 1, http=http)

    h = _headers_of(http.calls[0])
    assert h["Authorization"] == "Bearer env_token"
    assert h["Accept"] == "application/vnd.github+json"
    assert h["User-Agent"] == f"gitport/{__version__}"


def test_comment_requires_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(GithubError, match="token"):
        post_pr_comment(_report(), "a/b", 1, http=FakeHttp())


def test_comment_http_error_raises():
    http = FakeHttp()
    http.queue("GET", FakeResponse(403, text="forbidden"))
    with pytest.raises(GithubError, match="HTTP 403"):
        post_pr_comment(_report(), "a/b", 1, token=TOKEN, http=http)


# -- set_commit_status ---------------------------------------------------------


@pytest.mark.parametrize(("status", "state", "desc"), [
    ("PASSED", "success", "no blocking issues found"),
    ("WARNING", "success", "warnings found"),
    ("FAILED", "failure", "blocking issues found"),
])
def test_commit_status_mapping(status, state, desc):
    http = FakeHttp()
    http.queue("POST", FakeResponse(201, {"state": state}))

    out = set_commit_status(_report(status), "acme/app", "deadbeef",
                            token=TOKEN, http=http)

    assert out["state"] == state
    assert http.calls[0][1] == (
        "https://api.github.com/repos/acme/app/statuses/deadbeef")
    payload = http.calls[0][2]["json"]
    assert payload["state"] == state
    assert payload["context"] == "gitport"
    assert payload["description"] == desc


def test_commit_status_custom_context_and_error():
    http = FakeHttp()
    http.queue("POST", FakeResponse(422, text="bad sha"))
    with pytest.raises(GithubError):
        set_commit_status(_report(), "a/b", "badsha", token=TOKEN,
                          context="gitport/strict", http=http)


# -- CLI wiring -----------------------------------------------------------------


def _cli_report(monkeypatch, status):
    import gitport.cli as cli

    report = CheckReport(verdict=Verdict(
        status=status, breaking_changes_detected=(status == "FAILED"),
        summary="s"))
    monkeypatch.setattr(cli, "run_check", lambda *a, **k: report)
    return cli


def test_cli_post_comment_failure_warns_not_fails(tmp_path, monkeypatch):
    """A GitHub outage must not change the gate's exit code."""
    cli = _cli_report(monkeypatch, "PASSED")

    def boom(*a, **k):
        raise GithubError("api down")
    monkeypatch.setattr(cli, "post_pr_comment", boom)

    diff = tmp_path / "d.diff"
    diff.write_text("x")
    res = runner.invoke(cli.app, ["check", "--diff-file", str(diff),
                                  "--post-comment", "--pr", "5",
                                  "--repo-slug", "acme/app"])
    assert res.exit_code == 0, res.output
    assert "could not post PR comment" in res.output + (res.stderr or "")


def test_cli_post_comment_calls_github(tmp_path, monkeypatch):
    cli = _cli_report(monkeypatch, "PASSED")
    seen = {}
    monkeypatch.setattr(cli, "post_pr_comment",
                        lambda report, slug, pr: seen.update(slug=slug, pr=pr))

    diff = tmp_path / "d.diff"
    diff.write_text("x")
    res = runner.invoke(cli.app, ["check", "--diff-file", str(diff),
                                  "--post-comment", "--pr", "5",
                                  "--repo-slug", "acme/app"])
    assert res.exit_code == 0
    assert seen == {"slug": "acme/app", "pr": 5}


def test_cli_post_comment_without_pr_warns(tmp_path, monkeypatch):
    cli = _cli_report(monkeypatch, "PASSED")
    diff = tmp_path / "d.diff"
    diff.write_text("x")
    res = runner.invoke(cli.app, ["check", "--diff-file", str(diff),
                                  "--post-comment"])
    assert res.exit_code == 0  # warns and skips, never gates
    assert "needs --pr and --repo-slug" in res.output + (res.stderr or "")


def test_cli_set_status_calls_github(tmp_path, monkeypatch):
    cli = _cli_report(monkeypatch, "FAILED")
    seen = {}
    monkeypatch.setattr(cli, "set_commit_status",
                        lambda report, slug, sha: seen.update(slug=slug, sha=sha))

    diff = tmp_path / "d.diff"
    diff.write_text("x")
    res = runner.invoke(cli.app, ["check", "--diff-file", str(diff),
                                  "--set-status", "--sha", "abc123",
                                  "--repo-slug", "acme/app"])
    assert res.exit_code == 1  # FAILED verdict still gates
    assert seen == {"slug": "acme/app", "sha": "abc123"}
