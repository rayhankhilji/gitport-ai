"""Deterministic code analysis — the tools the Cohere agent can call.

These are real checks, not LLM guesses: the agent decides *which* to run and
*how to interpret* them, but the findings themselves come from parsers and
linters so the gate is grounded in facts.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Python AST analysis
# ---------------------------------------------------------------------------

# call -> why it's dangerous in production code
_DANGEROUS_CALLS = {
    "eval": "executes arbitrary code from a string",
    "exec": "executes arbitrary code",
    "compile": "dynamic code compilation; usually unnecessary",
    "__import__": "dynamic import that bypasses static analysis",
    "os.system": "shell command execution; prefer subprocess without shell=True",
    "os.popen": "shell command execution",
    "pickle.loads": "deserializing untrusted pickle data executes code",
    "pickle.load": "deserializing untrusted pickle data executes code",
    "marshal.loads": "unsafe deserialization",
    "yaml.load": "yaml.load without a SafeLoader executes arbitrary objects",
    "hashlib.md5": "md5 is broken; use sha256+ for security-sensitive hashing",
    "hashlib.sha1": "sha1 is broken for collision resistance",
    "tempfile.mktemp": "insecure temp file (race condition); use mkstemp",
}


def _dotted(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def analyze_python_source(source: str, filename: str = "<diff>") -> dict:
    """Parse Python source and report structure + dangerous call sites."""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as e:
        return {"ok": False, "file": filename, "syntax_error": f"{e.msg} (line {e.lineno})"}

    functions, classes, imports, dangerous = [], [], [], []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append({
                "name": node.name,
                "line": node.lineno,
                "args": [a.arg for a in node.args.args],
                "async": isinstance(node, ast.AsyncFunctionDef),
            })
        elif isinstance(node, ast.ClassDef):
            classes.append({"name": node.name, "line": node.lineno})
        elif isinstance(node, ast.Import):
            imports.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(f"{node.module or ''}.*" if node.names[0].name == "*"
                           else node.module or "")
        elif isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name in _DANGEROUS_CALLS:
                dangerous.append({
                    "line": node.lineno,
                    "call": name,
                    "why": _DANGEROUS_CALLS[name],
                })

    # shell=True is the thing that actually matters for subprocess calls
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _dotted(node.func).startswith("subprocess."):
            for kw in node.keywords:
                if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value:
                    dangerous.append({
                        "line": node.lineno,
                        "call": f"{_dotted(node.func)}(shell=True)",
                        "why": "shell=True enables command injection",
                    })

    return {
        "ok": True,
        "file": filename,
        "functions": functions,
        "classes": classes,
        "imports": sorted(set(filter(None, imports))),
        "dangerous_calls": dangerous,
    }


# ---------------------------------------------------------------------------
# SQL migration hazard lint
# ---------------------------------------------------------------------------

# (rule_id, severity, regex, why, suggestion)
_SQL_RULES = [
    ("DROP_TABLE", "critical",
     r"\bDROP\s+TABLE\b",
     "drops a table outright; destroys data and breaks anything still reading it",
     "rename to *_deprecated and drop after a full deploy cycle"),
    ("DROP_COLUMN", "critical",
     r"\bDROP\s+COLUMN\b",
     "old code versions still reading this column will crash during rollout",
     "deploy code that ignores the column first, then drop it in a later migration"),
    ("NOT_NULL_NO_DEFAULT", "high",
     r"\bADD\s+COLUMN\b[^;]*\bNOT\s+NULL\b(?![^;]*\bDEFAULT\b)",
     "adding NOT NULL without DEFAULT rewrites/locks the whole table on Postgres < 11",
     "add the column nullable or with a DEFAULT, backfill, then set NOT NULL"),
    ("ALTER_COLUMN_TYPE", "high",
     r"\bALTER\s+COLUMN\b[^;]*\bTYPE\b|\bALTER\s+COLUMN\b[^;]*\bSET\s+DATA\s+TYPE\b",
     "column type changes rewrite the table under an ACCESS EXCLUSIVE lock",
     "create a new column, dual-write, backfill, then swap"),
    ("INDEX_NO_CONCURRENTLY", "high",
     r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\b(?![^;]*\bCONCURRENTLY\b)",
     "CREATE INDEX without CONCURRENTLY blocks writes on the table",
     "use CREATE INDEX CONCURRENTLY (outside a transaction)"),
    ("FK_NO_NOT_VALID", "medium",
     r"\bADD\s+(?:CONSTRAINT\s+\w+\s+)?FOREIGN\s+KEY\b(?![^;]*\bNOT\s+VALID\b)",
     "adding a foreign key scans the whole table under a lock",
     "add with NOT VALID, then VALIDATE CONSTRAINT in a separate migration"),
    ("UNIQUE_NO_CONCURRENTLY", "medium",
     r"\bADD\s+CONSTRAINT\b[^;]*\bUNIQUE\b(?![^;]*\bUSING\s+INDEX\b)",
     "adding UNIQUE constraint directly takes an exclusive lock while scanning",
     "CREATE UNIQUE INDEX CONCURRENTLY then ADD CONSTRAINT ... USING INDEX"),
    ("RENAME_OBJECT", "medium",
     r"\bRENAME\s+(?:TABLE|COLUMN|TO)\b",
     "renames break old code versions still running during the deploy",
     "add the new name alongside, migrate reads/writes, remove the old name later"),
    ("UPDATE_NO_WHERE", "medium",
     r"\bUPDATE\s+\w+\s+SET\b(?![^;]*\bWHERE\b)",
     "table-wide UPDATE locks every row and can take minutes on large tables",
     "batch the update with a WHERE clause and key ranges"),
    ("DELETE_NO_WHERE", "high",
     r"\bDELETE\s+FROM\s+\w+(?![^;]*\bWHERE\b)",
     "unqualified DELETE removes every row",
     "add a WHERE clause; if intentional, use TRUNCATE and confirm backups"),
]


def lint_sql_migration(sql: str, filename: str = "<migration>") -> dict:
    """Statically lint SQL for operations that lock or destroy production tables."""
    findings = []
    for lineno, statement in enumerate(sql.splitlines(), start=1):
        upper = statement.upper()
        if upper.lstrip().startswith("--") or not statement.strip():
            continue
        for rule_id, severity, pattern, why, suggestion in _SQL_RULES:
            if re.search(pattern, upper):
                findings.append({
                    "rule": rule_id,
                    "severity": severity,
                    "line": lineno,
                    "statement": statement.strip()[:200],
                    "why": why,
                    "suggestion": suggestion,
                })
    return {
        "ok": True,
        "file": filename,
        "hazardous": any(f["severity"] in ("high", "critical") for f in findings),
        "findings": findings,
    }


# ---------------------------------------------------------------------------
# Dependency vulnerability scan (OSV)
# ---------------------------------------------------------------------------

_OSV_URL = "https://api.osv.dev/v1/querybatch"

_REQ_LINE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*(?:==|~=|>=|<=|>|<)\s*([A-Za-z0-9_.\-+!]+)")

_MANIFEST_ECOSYSTEMS = {
    "requirements.txt": "PyPI",
    "pyproject.toml": "PyPI",
    "poetry.lock": "PyPI",
    "package.json": "npm",
    "package-lock.json": "npm",
    "go.mod": "Go",
    "cargo.lock": "crates.io",
}


def detect_manifest(path: str) -> str | None:
    return _MANIFEST_ECOSYSTEMS.get(Path(path).name.lower())


def parse_pinned_deps(manifest_text: str) -> list[tuple[str, str]]:
    """Extract (name, version) pairs from a requirements-style manifest."""
    deps = []
    for line in manifest_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        if m := _REQ_LINE.match(line):
            deps.append((m.group(1), m.group(2)))
    return deps


def scan_dependencies(manifest_text: str, ecosystem: str = "PyPI",
                      timeout: float = 15.0, http=None) -> dict:
    """Query OSV for known vulnerabilities in pinned dependencies.

    Network-isolated: ``http`` is injectable so tests never hit the real API.
    """
    deps = parse_pinned_deps(manifest_text)
    if not deps:
        return {"ok": True, "ecosystem": ecosystem, "packages": 0, "vulnerabilities": []}

    queries = [
        {"package": {"name": name, "ecosystem": ecosystem}, "version": ver}
        for name, ver in deps
    ]
    try:
        if http is None:
            import httpx
            http = httpx.Client(timeout=timeout)
        resp = http.post(_OSV_URL, json={"queries": queries})
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except Exception as e:  # network hiccup must not break the gate
        return {"ok": False, "ecosystem": ecosystem, "packages": len(deps),
                "error": f"OSV query failed: {e}", "vulnerabilities": []}

    vulns = []
    for (name, ver), result in zip(deps, results):
        for v in (result or {}).get("vulns") or []:
            vulns.append({
                "package": name,
                "version": ver,
                "id": v.get("id", ""),
                "summary": v.get("summary", "")[:300],
                "severity": _osv_severity(v),
            })
    return {"ok": True, "ecosystem": ecosystem, "packages": len(deps),
            "vulnerabilities": vulns}


def _osv_severity(vuln: dict) -> str:
    scores = vuln.get("severity") or []
    if scores:
        return scores[0].get("score", "unknown")
    return "unknown"


def manifest_summary(manifest_text: str) -> str:
    deps = parse_pinned_deps(manifest_text)
    return json.dumps({"packages": len(deps), "names": [n for n, _ in deps]})
