"""Deterministic lints for infra-as-code files: Dockerfiles and GitHub Actions.

These files are short and declarative, so per-line regex rules plus a couple
of whole-file checks (missing USER, pull_request_target + head checkout)
catch the classic production footguns without needing a parser.
"""

from __future__ import annotations

import re
from pathlib import Path

_WORKFLOW_PATH = re.compile(r"(^|/)\.github/workflows/[^/]+\.ya?ml$")


def _config_kind(filename: str) -> str | None:
    name = Path(filename).name.lower()
    if name == "dockerfile" or name.startswith("dockerfile.") or name.endswith(".dockerfile"):
        return "dockerfile"
    if _WORKFLOW_PATH.search(filename.replace("\\", "/")):
        return "github-actions"
    return None


# --- Dockerfile --------------------------------------------------------------

_FROM_RE = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)", re.IGNORECASE)
_USER_RE = re.compile(r"^\s*USER\s+(\S+)", re.IGNORECASE)

# (rule, severity, regex, why, suggestion)
_DOCKER_RULES = [
    ("remote-add", "high",
     re.compile(r"^\s*ADD\s+https?://", re.IGNORECASE),
     "ADD of a remote URL fetches unverified content at build time",
     "use COPY for local files, or curl + checksum verification for remote ones"),
    ("pipe-to-shell", "high",
     re.compile(r"\b(?:curl|wget)\b[^|]*\|\s*(?:sudo\s+)?(?:ba)?sh\b"),
     "piping a remote script straight into a shell runs it sight-unseen",
     "download the script, verify a checksum/signature, then execute it"),
    ("env-secret", "high",
     re.compile(r"^\s*(?:ENV|ARG)\s+\w*(?:_KEY|SECRET|PASSWORD|PASSWD|TOKEN)\w*\s*(?:=|\s+)\S+",
                re.IGNORECASE),
     "secret value baked into the Dockerfile lands in image layers and build logs",
     "pass it with --mount=type=secret / build args, never as a literal"),
]


def _lint_dockerfile(source: str, filename: str) -> dict:
    lines = source.splitlines()
    findings = []
    has_user = False
    for lineno, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for rule, severity, pattern, why, suggestion in _DOCKER_RULES:
            if pattern.search(line):
                findings.append({"line": lineno, "rule": rule, "severity": severity,
                                 "why": why, "suggestion": suggestion})
        if m := _FROM_RE.match(line):
            image = m.group(1)
            if image.startswith("$") or image == "scratch" or "@" in image:
                continue  # build-arg base, scratch, or digest-pinned
            if ":" not in image or image.rsplit(":", 1)[1] == "latest":
                findings.append({
                    "line": lineno, "rule": "unpinned-base-image", "severity": "medium",
                    "why": f"base image '{image}' is not pinned to a tag/digest",
                    "suggestion": "pin a version tag (ubuntu:24.04) or a sha256 digest",
                })
        elif m := _USER_RE.match(line):
            has_user = True
            if m.group(1).lower() in ("root", "0"):
                findings.append({
                    "line": lineno, "rule": "root-user", "severity": "medium",
                    "why": "USER is explicitly root — the container runs with full privileges",
                    "suggestion": "create and switch to a non-root user",
                })
    if not has_user:
        findings.append({
            "line": len(lines), "rule": "missing-user", "severity": "medium",
            "why": "no USER directive — the container runs as root",
            "suggestion": "add a non-root USER before the entrypoint",
        })
    return {"ok": True, "file": filename, "kind": "dockerfile",
            "hazardous": any(f["severity"] in ("high", "critical") for f in findings),
            "findings": findings}


# --- GitHub Actions ----------------------------------------------------------

_USES_RE = re.compile(r"^\s*-?\s*uses\s*:\s*([\w./-]+)@(\S+)")
_SHA_RE = re.compile(r"[0-9a-fA-F]{40}")
_RUN_RE = re.compile(r"^(\s*)-?\s*run\s*:\s*(.*)")
_GITHUB_EVENT_RE = re.compile(r"\$\{\{\s*github\.event\.")
_HEAD_REF_RE = re.compile(r"\$\{\{\s*github\.(?:event\.pull_request\.head|head_ref)")


def _lint_github_actions(source: str, filename: str) -> dict:
    lines = source.splitlines()
    findings = []
    has_pr_target = any("pull_request_target" in line for line in lines)
    has_checkout = "actions/checkout" in source

    run_indent = None  # indent of the `run:` key whose block body we're inside
    for lineno, line in enumerate(lines, start=1):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())

        if m := _USES_RE.match(line):
            action, ref = m.group(1), m.group(2)
            if not _SHA_RE.fullmatch(ref):
                findings.append({
                    "line": lineno, "rule": "unpinned-action", "severity": "medium",
                    "why": f"{action}@{ref} is a mutable ref; a compromised tag ships code to CI",
                    "suggestion": "pin to the commit SHA (@<40-hex>) and keep the version in a comment",
                })

        if has_pr_target and has_checkout and _HEAD_REF_RE.search(line):
            findings.append({
                "line": lineno, "rule": "pull-request-target-checkout", "severity": "high",
                "why": ("pull_request_target runs with a write token on the base repo; "
                        "checking out the PR head executes attacker-controlled code there"),
                "suggestion": "use pull_request, or never checkout github.event.pull_request.head",
            })

        # `run:` script injection — inline value or indented block scalar body;
        # blank lines keep a literal block open (only a dedented key ends it)
        if run_indent is not None and stripped and indent <= run_indent:
            run_indent = None
        if run_indent is not None:
            if _GITHUB_EVENT_RE.search(line):
                findings.append({
                    "line": lineno, "rule": "script-injection", "severity": "high",
                    "why": "github.event.* data interpolated into a run script enables shell injection",
                    "suggestion": "pass it via an env: var and quote \"$VAR\" in the script",
                })
        elif m := _RUN_RE.match(line):
            body = m.group(2)
            if not body or body[0] in "|>":
                run_indent = indent
            elif _GITHUB_EVENT_RE.search(body):
                findings.append({
                    "line": lineno, "rule": "script-injection", "severity": "high",
                    "why": "github.event.* data interpolated into a run script enables shell injection",
                    "suggestion": "pass it via an env: var and quote \"$VAR\" in the script",
                })

    return {"ok": True, "file": filename, "kind": "github-actions",
            "hazardous": any(f["severity"] in ("high", "critical") for f in findings),
            "findings": findings}


def lint_config(source: str, filename: str) -> dict:
    """Lint a Dockerfile or GitHub Actions workflow for infra footguns."""
    kind = _config_kind(filename)
    if kind == "dockerfile":
        return _lint_dockerfile(source, filename)
    if kind == "github-actions":
        return _lint_github_actions(source, filename)
    return {"ok": False, "file": filename, "kind": None, "findings": [],
            "error": "unrecognized config type (expected Dockerfile or .github/workflows/*.yml)"}
