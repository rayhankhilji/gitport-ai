"""Secret scanning for changed files and diff fragments.

Each finding is deterministic — a named regex or a Shannon-entropy threshold —
and only ever carries a 6-char preview, never the full secret, so tool output
is safe to hand to the model and to store in the audit trail.
"""

from __future__ import annotations

import math
import re
from collections import Counter

# (kind, regex, suggestion) — severity is always "high" for a committed secret.
_SECRET_RULES = [
    ("aws-access-key",
     re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
     "revoke the key in IAM and move it to a secrets manager or env var"),
    ("github-token",
     re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"),
     "revoke the token in GitHub settings and use a secret store or GITHUB_TOKEN"),
    ("slack-token",
     re.compile(r"\bxox[baprs]-[A-Za-z0-9-]+"),
     "revoke the token in the Slack app console and load it from the environment"),
    ("stripe-live-key",
     re.compile(r"\b[sr]k_live_[A-Za-z0-9]+"),
     "roll the key in the Stripe dashboard; live keys charge real cards"),
    ("google-api-key",
     re.compile(r"\bAIza[0-9A-Za-z\-_]{35}"),
     "restrict/regenerate the key in Google Cloud console"),
    ("private-key",
     re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
     "remove the key material from the repo; rotate it — it must be assumed leaked"),
    ("jwt",
     re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
     "treat the token as compromised; never commit live session or API tokens"),
    ("generic-secret",
     re.compile(r"[\w.-]*(?:password|passwd|secret|api_?key|token)[\w.-]*\s*[:=]\s*[\"'][^\"']{8,}[\"']",
                re.IGNORECASE),
     "read the value from the environment or a secrets manager instead of a literal"),
]

_ENTROPY_CANDIDATE = re.compile(r"[A-Za-z0-9+/=_-]{20,}")
_ENTROPY_MIN = 4.5

# Lines whose tokens are typically hashes, URLs or file ids — entropy here is
# expected, not a leaked secret.
_ENTROPY_SKIP_LINE = re.compile(
    r"://|\b(?:sha-?1|sha-?256|sha-?384|sha-?512|md5|integrity|checksum|digest|fingerprint)\b",
    re.IGNORECASE,
)
_HEX_TOKEN = re.compile(r"[0-9a-fA-F]+")


def _shannon_entropy(s: str) -> float:
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in Counter(s).values())


def scan_text_for_secrets(text: str, filename: str = "<diff>") -> dict:
    """Scan text line by line for committed credentials.

    Returns {"ok", "file", "findings", "scanned_lines"}; every finding is
    {file, line, kind, match_preview, severity, suggestion} where
    match_preview is at most the first 6 characters of the match.
    """
    lines = text.splitlines()
    findings = []
    covered = set()  # lines that already produced a named-rule finding
    for lineno, line in enumerate(lines, start=1):
        for kind, pattern, suggestion in _SECRET_RULES:
            for m in pattern.finditer(line):
                findings.append({
                    "file": filename,
                    "line": lineno,
                    "kind": kind,
                    "match_preview": m.group(0)[:6] + "…",
                    "severity": "high",
                    "suggestion": suggestion,
                })
                covered.add(lineno)

    for lineno, line in enumerate(lines, start=1):
        if lineno in covered or _ENTROPY_SKIP_LINE.search(line):
            continue
        seen = set()
        for m in _ENTROPY_CANDIDATE.finditer(line):
            token = m.group(0)
            if token in seen:
                continue
            seen.add(token)
            # pure hex/digits are hashes or ids, not keys
            if _HEX_TOKEN.fullmatch(token) or token.isdigit():
                continue
            # real secrets mix character classes; upper+underscore constants don't
            classes = sum((re.search(r"[a-z]", token) is not None,
                           re.search(r"[A-Z]", token) is not None,
                           re.search(r"[0-9]", token) is not None,
                           re.search(r"[+/=_-]", token) is not None))
            if classes < 3 or _shannon_entropy(token) <= _ENTROPY_MIN:
                continue
            findings.append({
                "file": filename,
                "line": lineno,
                "kind": "high-entropy-string",
                "match_preview": token[:6] + "…",
                "severity": "high",
                "suggestion": "looks like a random credential; confirm and rotate or remove it",
            })
    findings.sort(key=lambda f: f["line"])
    return {
        "ok": True,
        "file": filename,
        "findings": findings,
        "scanned_lines": len(lines),
    }
