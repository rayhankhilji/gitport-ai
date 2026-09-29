"""GitHub integration: upserted PR comments and commit statuses.

Every public function takes an injectable ``http`` object (anything with
.get/.post/.patch, e.g. httpx.Client) so tests never touch the network.
Authentication is a bearer token passed explicitly or read from GITHUB_TOKEN.
"""

from __future__ import annotations

import os

import httpx

from . import __version__
from .formatters import render
from .models import CheckReport

_API = "https://api.github.com"
_MARKER = "<!-- gitport -->"

_STATE = {"PASSED": "success", "WARNING": "success", "FAILED": "failure"}
_DESCRIPTION = {
    "PASSED": "no blocking issues found",
    "WARNING": "warnings found",
    "FAILED": "blocking issues found",
}


class GithubError(Exception):
    """Raised when a GitHub API call cannot be completed."""


def post_pr_comment(report: CheckReport, repo_slug: str, pr_number: int,
                    token: str | None = None, http=None) -> dict:
    """Create or update the gitport comment on a pull request.

    The comment body carries a ``<!-- gitport -->`` marker; if an existing
    comment contains the marker it is PATCHed instead of posting a duplicate.
    Returns ``{"action": "created"|"updated", "comment": <api response>}``.
    """
    tok = _token(token)
    if http is None:
        with httpx.Client(timeout=15.0) as client:
            return _post_pr_comment(client, report, repo_slug, pr_number, tok)
    return _post_pr_comment(http, report, repo_slug, pr_number, tok)


def set_commit_status(report: CheckReport, repo_slug: str, sha: str,
                      token: str | None = None, context: str = "gitport",
                      http=None) -> dict:
    """POST a commit status: PASSED/WARNING→success, FAILED→failure.

    Returns ``{"state": <github state>, "status": <api response>}``.
    """
    tok = _token(token)
    if http is None:
        with httpx.Client(timeout=15.0) as client:
            return _set_commit_status(client, report, repo_slug, sha, tok, context)
    return _set_commit_status(http, report, repo_slug, sha, tok, context)


def _token(token: str | None) -> str:
    tok = token or os.environ.get("GITHUB_TOKEN", "")
    if not tok:
        raise GithubError("no GitHub token — pass token= or set GITHUB_TOKEN")
    return tok


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": f"gitport/{__version__}",
    }


def _check(resp, what: str):
    """Return the decoded body, or raise GithubError on HTTP >= 400."""
    if getattr(resp, "status_code", 200) >= 400:
        detail = getattr(resp, "text", "") or ""
        raise GithubError(f"{what} failed: HTTP {resp.status_code} {detail[:200]}".strip())
    return resp.json()


def _post_pr_comment(http, report: CheckReport, repo_slug: str,
                     pr_number: int, token: str) -> dict:
    headers = _headers(token)
    url = f"{_API}/repos/{repo_slug}/issues/{pr_number}/comments"
    body = f"{_MARKER}\n{render(report, 'markdown')}"

    comments = _check(http.get(url, headers=headers), "list PR comments")
    existing = None
    if isinstance(comments, list):
        existing = next((c for c in comments if _MARKER in (c.get("body") or "")), None)

    if existing is not None:
        patch_url = f"{_API}/repos/{repo_slug}/issues/comments/{existing['id']}"
        data = _check(http.patch(patch_url, headers=headers, json={"body": body}),
                      "update PR comment")
        return {"action": "updated", "comment": data}
    data = _check(http.post(url, headers=headers, json={"body": body}),
                  "create PR comment")
    return {"action": "created", "comment": data}


def _set_commit_status(http, report: CheckReport, repo_slug: str,
                       sha: str, token: str, context: str) -> dict:
    status = report.verdict.status
    payload = {
        "state": _STATE.get(status, "failure"),
        "context": context,
        "description": _DESCRIPTION.get(status, status.lower()),
    }
    url = f"{_API}/repos/{repo_slug}/statuses/{sha}"
    data = _check(http.post(url, headers=_headers(token), json=payload),
                  "set commit status")
    return {"state": payload["state"], "status": data}
