"""Tool definitions and dispatch for the Cohere agent loop.

The model decides which tools to call and in what order; this module owns the
JSON schemas sent to ``cohere.chat`` and the safe execution of each call.
Executors resolve a file's *post-change* content from the repo working tree
when available, falling back to the lines added by the diff — so the same
tools work inside a repo, in CI, or against a bare diff sent over the API.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .analysis import (
    analyze_python_source,
    detect_manifest,
    lint_sql_migration,
    scan_dependencies,
)
from .detectors.confchecks import lint_config
from .detectors.langs import detect_language, scan_source_patterns
from .detectors.secrets import scan_text_for_secrets
from .models import FileDiff, ToolCallRecord

_MAX_FILE_CHARS = 40_000
_SQL_SUFFIXES = {".sql"}
_MIGRATION_HINTS = ("migration", "alembic", "migrate", "ddl")


@dataclass
class ToolContext:
    """Everything an executor is allowed to touch."""

    file_diffs: dict[str, FileDiff] = field(default_factory=dict)
    repo_root: Path | None = None

    def source_for(self, path: str) -> tuple[str, str] | None:
        """Return (source_text, provenance) for a changed path, or None."""
        if self.repo_root:
            candidate = (self.repo_root / path).resolve()
            try:
                candidate.relative_to(self.repo_root.resolve())
            except ValueError:
                return None  # path escapes the repo — refuse
            if candidate.is_file() and candidate.stat().st_size < _MAX_FILE_CHARS:
                return candidate.read_text(encoding="utf-8", errors="replace"), "repo"
        fd = self.file_diffs.get(path)
        if fd is not None:
            return fd.added_source, "diff"
        return None

    def added_source_for(self, path: str) -> str | None:
        """Only the lines this diff adds — merge-gate findings belong to the
        change, not to history."""
        fd = self.file_diffs.get(path)
        return fd.added_source if fd is not None else None


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


TOOL_SCHEMAS = [
    _fn(
        "analyze_python_file",
        "Parse a changed Python file with the AST and report its functions, "
        "imports, and dangerous call sites (eval/exec, subprocess shell=True, "
        "unsafe deserialization, weak crypto).",
        {"path": {"type": "string", "description": "Repo-relative path of a changed file"}},
        ["path"],
    ),
    _fn(
        "lint_migration_file",
        "Statically lint a changed SQL file or migration for operations that "
        "lock or destroy production tables (NOT NULL without DEFAULT, DROP "
        "COLUMN, CREATE INDEX without CONCURRENTLY, unqualified UPDATE/DELETE).",
        {"path": {"type": "string", "description": "Repo-relative path of a changed file"}},
        ["path"],
    ),
    _fn(
        "scan_manifest",
        "Scan a changed dependency manifest (requirements.txt, pyproject.toml, "
        "package.json, go.mod, Cargo.lock) against the OSV vulnerability "
        "database and report known CVEs in pinned versions.",
        {"path": {"type": "string", "description": "Repo-relative path of a changed manifest"}},
        ["path"],
    ),
    _fn(
        "read_file",
        "Read a file from the repository (post-change state) to gather context "
        "around a suspicious change. Output is truncated to 40KB.",
        {"path": {"type": "string", "description": "Repo-relative path to read"}},
        ["path"],
    ),
    _fn(
        "scan_secrets",
        "Scan a changed file's added lines for leaked credentials: API keys, "
        "private keys, tokens, JWTs, hardcoded passwords, high-entropy "
        "strings. Matches are masked in the output.",
        {"path": {"type": "string", "description": "Repo-relative path of a changed file"}},
        ["path"],
    ),
    _fn(
        "scan_source_patterns",
        "Detect dangerous language-specific patterns in changed non-Python "
        "source files (JS/TS, Go, Java, Ruby, PHP): eval, innerHTML sinks, "
        "command execution, unsafe deserialization, leftover debugger "
        "statements. Operates on the diff's added lines.",
        {"path": {"type": "string", "description": "Repo-relative path of a changed file"}},
        ["path"],
    ),
    _fn(
        "lint_config_file",
        "Lint changed Dockerfile or GitHub Actions workflow files for "
        "supply-chain and privilege hazards: unpinned bases/actions, "
        "curl-piped-to-shell installs, missing USER, script injection via "
        "github.event interpolation. Operates on the diff's added lines.",
        {"path": {"type": "string", "description": "Repo-relative path of a changed file"}},
        ["path"],
    ),
]


def execute_tool(ctx: ToolContext, name: str, arguments: dict) -> dict:
    """Run one tool call. Never raises — errors are data the agent can see."""
    try:
        if name == "analyze_python_file":
            return _t_analyze_python(ctx, arguments.get("path", ""))
        if name == "lint_migration_file":
            return _t_lint_migration(ctx, arguments.get("path", ""))
        if name == "scan_manifest":
            return _t_scan_manifest(ctx, arguments.get("path", ""))
        if name == "read_file":
            return _t_read_file(ctx, arguments.get("path", ""))
        if name == "scan_secrets":
            return _t_scan_secrets(ctx, arguments.get("path", ""))
        if name == "scan_source_patterns":
            return _t_scan_patterns(ctx, arguments.get("path", ""))
        if name == "lint_config_file":
            return _t_lint_config(ctx, arguments.get("path", ""))
        return {"ok": False, "error": f"unknown tool: {name}"}
    except Exception as e:
        return {"ok": False, "error": f"{name} failed: {e}"}


def _t_analyze_python(ctx: ToolContext, path: str) -> dict:
    if not path.endswith(".py"):
        return {"ok": False, "error": "not a python file", "file": path}
    found = ctx.source_for(path)
    if found is None:
        return {"ok": False, "error": "file not in diff and not readable", "file": path}
    source, provenance = found
    result = analyze_python_source(source, filename=path)
    result["source"] = provenance
    return result


def _t_lint_migration(ctx: ToolContext, path: str) -> dict:
    is_migration = (
        Path(path).suffix.lower() in _SQL_SUFFIXES
        or any(h in path.lower() for h in _MIGRATION_HINTS)
    )
    if not is_migration:
        return {"ok": False, "error": "not a sql/migration file", "file": path}
    found = ctx.source_for(path)
    if found is None:
        return {"ok": False, "error": "file not in diff and not readable", "file": path}
    source, provenance = found
    result = lint_sql_migration(source, filename=path)
    result["source"] = provenance
    return result


def _t_scan_manifest(ctx: ToolContext, path: str) -> dict:
    ecosystem = detect_manifest(path)
    if ecosystem is None:
        return {"ok": False, "error": "unrecognized manifest type", "file": path}
    found = ctx.source_for(path)
    if found is None:
        return {"ok": False, "error": "file not in diff and not readable", "file": path}
    source, _ = found
    result = scan_dependencies(source, ecosystem=ecosystem)
    result["file"] = path
    return result


def _t_read_file(ctx: ToolContext, path: str) -> dict:
    found = ctx.source_for(path)
    if found is None:
        return {"ok": False, "error": "not readable (missing or outside repo)", "file": path}
    source, provenance = found
    return {"ok": True, "file": path, "source": provenance,
            "content": source[:_MAX_FILE_CHARS]}


def _t_scan_secrets(ctx: ToolContext, path: str) -> dict:
    source = ctx.added_source_for(path)
    if source is None:
        return {"ok": False, "error": "file not in diff", "file": path}
    return scan_text_for_secrets(source, filename=path)


def _t_scan_patterns(ctx: ToolContext, path: str) -> dict:
    lang = detect_language(path)
    if lang in (None, "python"):  # .py goes through analyze_python_file (AST)
        return {"ok": False, "error": "unsupported language for this scanner",
                "file": path}
    source = ctx.added_source_for(path)
    if source is None:
        return {"ok": False, "error": "file not in diff", "file": path}
    return scan_source_patterns(source, filename=path)


def _t_lint_config(ctx: ToolContext, path: str) -> dict:
    name = Path(path).name
    is_cfg = name in ("Dockerfile",) or name.startswith("Dockerfile.") \
        or ".github/workflows/" in path.replace("\\", "/")
    if not is_cfg:
        return {"ok": False, "error": "not a Dockerfile/CI workflow file", "file": path}
    source = ctx.added_source_for(path)
    if source is None:
        return {"ok": False, "error": "file not in diff", "file": path}
    return lint_config(source, filename=path)


def summarize_result(result: dict) -> str:
    """One-line audit-trail summary for a tool result."""
    if not result.get("ok"):
        return f"error: {result.get('error', 'unknown')}"
    if "vulnerabilities" in result:
        return f"{result.get('packages', 0)} pkgs, {len(result['vulnerabilities'])} vulns"
    if "findings" in result:
        return f"{len(result['findings'])} findings"
    if "dangerous_calls" in result:
        return f"{len(result.get('functions', []))} fns, {len(result['dangerous_calls'])} dangerous calls"
    if "content" in result:
        return f"{len(result['content'])} chars"
    return "ok"


def record(name: str, arguments: dict, result: dict) -> ToolCallRecord:
    return ToolCallRecord(
        name=name,
        arguments=arguments,
        ok=bool(result.get("ok", True)),
        summary=summarize_result(result),
    )


def tool_message_content(result: dict) -> list[dict]:
    """Wrap a result as Cohere document blocks (enables citations)."""
    return [{"type": "document", "document": {"data": json.dumps(result, default=str)}}]
