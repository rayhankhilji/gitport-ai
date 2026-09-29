"""Language detection and dangerous-pattern scanning for non-Python sources.

Python files get real AST analysis in gitport.analysis; this module covers
the languages a diff commonly mixes in — JS/TS, Go, Java, Ruby, PHP — with
deterministic per-line regex rules so findings are grounded, not guessed.
"""

from __future__ import annotations

import re
from pathlib import Path

_EXT_LANGUAGE = {
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".go": "go",
    ".java": "java",
    ".rb": "ruby",
    ".php": "php",
    ".py": "python",
}


def detect_language(path) -> str | None:
    """Map a file path's extension to a language name, or None if unknown."""
    return _EXT_LANGUAGE.get(Path(path).suffix.lower())


# (pattern, severity, regex, why)
_JS_TS_RULES = [
    ("eval", "high",
     r"\beval\s*\(",
     "eval() executes arbitrary strings as code"),
    ("new-function", "high",
     r"\bnew\s+Function\s*\(",
     "the Function constructor evaluates string bodies like eval"),
    ("document-write", "medium",
     r"\bdocument\.write(?:ln)?\s*\(",
     "document.write injects unsanitized markup; classic DOM-XSS sink"),
    ("innerhtml", "medium",
     r"\binnerHTML\s*=",
     "innerHTML assignment of unsanitized data enables DOM XSS; prefer textContent"),
    ("dangerously-set-inner-html", "medium",
     r"\bdangerouslySetInnerHTML",
     "React dangerouslySetInnerHTML bypasses XSS escaping; sanitize the input"),
    ("child-process", "medium",
     r"(?:require\s*\(\s*[\"']child_process[\"']|from\s+[\"']child_process[\"']|child_process\s*\.)",
     "child_process usage shells out to the OS; review exec/spawn arguments"),
    ("localstorage-secret", "medium",
     r"\blocalStorage\b[^;]*(?i:token|secret|password|api_?key)",
     "credentials in localStorage are readable by any script on the page (XSS theft)"),
    ("debugger", "medium",
     r"\bdebugger\b\s*;?",
     "debugger statement left in source; halts execution under devtools"),
    ("console-log", "low",
     r"\bconsole\.log\s*\(",
     "console.log left in source; can leak data and pollutes logs"),
]

# Bare exec-family calls are only suspicious when child_process is imported —
# otherwise regex.exec(...) would false-positive everywhere.
_JS_EXEC_CALL = ("shell-exec", "high",
                 r"(?<![\w.])(?:exec|execSync|execFile|execFileSync|spawn|spawnSync|fork)\s*\(",
                 "exec/spawn runs an OS command; unescaped input enables command injection")

_GO_RULES = [
    ("exec-sprintf", "high",
     r"\bexec\.Command\s*\([^)]*fmt\.Sprintf",
     "building a command with fmt.Sprintf inside exec.Command enables command injection"),
    ("unsafe", "medium",
     r"\bunsafe\.",
     "unsafe.Pointer operations bypass Go memory safety"),
    ("http-no-timeouts", "medium",
     r"\bhttp\.ListenAndServe\s*\(",
     "http.ListenAndServe sets no timeouts (Slowloris); use http.Server with ReadHeaderTimeout"),
]

_JAVA_RULES = [
    ("runtime-exec", "high",
     r"Runtime\.getRuntime\s*\(\s*\)\s*\.exec\s*\(",
     "Runtime.exec runs an OS command; unescaped input enables command injection"),
    ("objectinputstream", "high",
     r"\bnew\s+ObjectInputStream\s*\(",
     "deserializing untrusted ObjectInputStream data executes arbitrary classes"),
    ("weak-cipher", "high",
     r"Cipher\.getInstance\s*\(\s*[\"'][^\"']*(?:DES|ECB)",
     "DES/ECB are weak crypto; use AES/GCM"),
]

_RB_RULES = [
    ("eval", "high",
     r"\beval\s*\(",
     "eval() executes arbitrary strings as code"),
    ("system", "high",
     r"(?<![.\w])system\s*\(",
     "system() runs a shell command; unescaped input enables command injection"),
    ("backticks", "high",
     r"`[^`\n]+`",
     "backticks execute a shell command; unescaped input enables command injection"),
    ("binding-pry", "medium",
     r"\bbinding\.pry\b",
     "pry debugger left in source; halts execution"),
]

_PHP_RULES = [
    ("eval", "high",
     r"\beval\s*\(",
     "eval() executes arbitrary strings as code"),
    ("shell-exec", "high",
     r"\bshell_exec\s*\(",
     "shell_exec() runs a shell command; unescaped input enables command injection"),
    ("unserialize", "high",
     r"\bunserialize\s*\(",
     "unserialize() on untrusted data enables PHP object injection"),
]

_PY_RULES = [
    ("pdb", "medium",
     r"\bpdb\.set_trace\s*\(",
     "pdb breakpoint left in source; halts the process"),
    ("breakpoint", "medium",
     r"\bbreakpoint\s*\(",
     "breakpoint() left in source; drops into a debugger"),
]

_RULES = {
    "javascript": _JS_TS_RULES,
    "typescript": _JS_TS_RULES,
    "go": _GO_RULES,
    "java": _JAVA_RULES,
    "ruby": _RB_RULES,
    "php": _PHP_RULES,
    "python": _PY_RULES,
}


def scan_source_patterns(source: str, filename: str) -> dict:
    """Scan source for language-specific dangerous constructs.

    Returns {"ok", "file", "language", "findings"}; findings carry
    {line, pattern, why, severity}. Unknown file types scan clean.
    """
    language = detect_language(filename)
    rules = list(_RULES.get(language, []))
    if language in ("javascript", "typescript") and "child_process" in source:
        rules.append(_JS_EXEC_CALL)

    findings = []
    for lineno, line in enumerate(source.splitlines(), start=1):
        for pattern, severity, regex, why in rules:
            if re.search(regex, line):
                findings.append({
                    "line": lineno,
                    "pattern": pattern,
                    "severity": severity,
                    "why": why,
                })
    return {
        "ok": True,
        "file": filename,
        "language": language,
        "findings": findings,
    }
