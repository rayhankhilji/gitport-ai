"""Diff ingestion and unified-diff parsing.

The gatekeeper works on raw unified diffs — either produced by ``git diff``
inside a repository or supplied directly (API / MCP / pre-receive hook).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .models import AddedLine, FileDiff

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


class GitError(RuntimeError):
    pass


# Well-known hash of git's empty tree — used as the base for a diff when a
# push creates a brand-new ref (no remote ancestor exists).
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def git_diff(repo: str | Path, base: str | None = None, head: str | None = None,
             staged: bool = False) -> str:
    """Return the unified diff for a range, refs, or staged changes."""
    if staged:
        args = ["diff", "--staged", "--unified=3"]
    elif base and head:
        # base...head diffs merge-base→head; the empty tree has no merge base,
        # so new-ref diffs must compare the two trees directly.
        args = ["diff", "--unified=3", base, head] if base == EMPTY_TREE \
            else ["diff", "--unified=3", f"{base}...{head}"]
    elif base:
        args = ["diff", "--unified=3", base]
    else:
        args = ["diff", "--unified=3", "HEAD"]
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip() or 'unknown error'}")
    return proc.stdout


def parse_unified_diff(text: str) -> list[FileDiff]:
    """Parse a unified diff into per-file structures with added-line numbers.

    Tracks new-file line numbers so reported findings can point at the exact
    line a reviewer (or CI annotation) should land on.
    """
    files: list[FileDiff] = []
    current: FileDiff | None = None
    new_lineno = 0

    for raw in text.splitlines():
        if raw.startswith("diff --git"):
            current = FileDiff(path="")
            files.append(current)
            continue
        if current is None:
            continue

        if raw.startswith("Binary files"):
            current.is_binary = True
        elif raw.startswith("new file mode"):
            current.is_new = True
        elif raw.startswith("deleted file mode"):
            current.is_deleted = True
        elif raw.startswith("rename from "):
            current.old_path = raw.removeprefix("rename from ").strip()
        elif raw.startswith("rename to "):
            current.path = raw.removeprefix("rename to ").strip()
        elif raw.startswith("--- "):
            p = raw[4:].strip()
            if p != "/dev/null":
                current.old_path = _strip_prefix(p)
        elif raw.startswith("+++ "):
            p = raw[4:].strip()
            if p != "/dev/null":
                current.path = _strip_prefix(p)
            else:
                current.is_deleted = True
        elif m := _HUNK_RE.match(raw):
            new_lineno = int(m.group(2))
        elif raw.startswith("+") and not raw.startswith("+++"):
            current.added.append(AddedLine(lineno=new_lineno, text=raw[1:]))
            new_lineno += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            current.deleted_count += 1
        elif raw.startswith(" "):
            new_lineno += 1
        # "\ No newline at end of file" and blank lines need no tracking.

    return [f for f in files if f.path or f.is_deleted]


def _strip_prefix(path: str) -> str:
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[2:]
    return path


def diff_digest(files: list[FileDiff], max_chars: int) -> str:
    """Compact, prompt-friendly rendering of the diff.

    Shows each changed file with its added lines and real line numbers,
    truncated to ``max_chars`` so huge diffs stay inside context limits.
    """
    parts: list[str] = []
    for f in files:
        status = "new" if f.is_new else "deleted" if f.is_deleted else "modified"
        header = f"### {f.path} ({status}, +{len(f.added)}/-{f.deleted_count})"
        if f.is_binary:
            parts.append(f"{header}\n<binary file>")
            continue
        body = "\n".join(f"{ln.lineno:>5} + {ln.text}" for ln in f.added)
        parts.append(f"{header}\n{body}")

    digest = "\n\n".join(parts) or "<empty diff>"
    if len(digest) > max_chars:
        digest = digest[:max_chars] + "\n\n<diff truncated>"
    return digest
