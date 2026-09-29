"""Policy-as-code: .gitport/policy.toml tunes the gate without code changes.

Schema:

    severity_floor = "medium"     # drop flaws below this severity (default "low")
    fail_on = ["FAILED"]          # verdict statuses that block the merge

    [[suppress]]                  # known-risk acceptances
    glob = "vendor/**"            #   fnmatch on flaw.file
    match = "substring"           #   optional substring of flaw.issue
    reason = "..."                #   recorded in the verdict summary for audit

    [[escalate]]                  # hot zones
    glob = "migrations/**"        #   flaws in matching paths bump to "high"
"""

from __future__ import annotations

import fnmatch
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .models import Flaw, Verdict

Severity = Literal["low", "medium", "high", "critical"]
Status = Literal["PASSED", "WARNING", "FAILED"]

_SEVERITY_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2, "critical": 3}


class PolicyError(Exception):
    """Raised when a policy file exists but cannot be parsed or validated."""


class SuppressRule(BaseModel):
    """A known-risk acceptance: matching flaws are dropped and counted."""

    model_config = ConfigDict(extra="forbid")

    glob: str
    match: str | None = None
    reason: str = ""


class EscalateRule(BaseModel):
    """A hot zone: flaws in matching paths are bumped to high severity."""

    model_config = ConfigDict(extra="forbid")

    glob: str


class Policy(BaseModel):
    """Parsed policy.toml. Defaults reproduce the plain gate behavior."""

    model_config = ConfigDict(extra="forbid")

    severity_floor: Severity = "low"
    fail_on: list[Status] = Field(default_factory=lambda: ["FAILED"])
    suppress: list[SuppressRule] = Field(default_factory=list)
    escalate: list[EscalateRule] = Field(default_factory=list)


def find_policy_path(cfg, repo: str | Path = ".") -> Path | None:
    """Resolve ``cfg.policy_path`` against the repo root; None when absent."""
    candidate = Path(cfg.policy_path)
    if not candidate.is_absolute():
        candidate = Path(repo) / candidate
    return candidate if candidate.is_file() else None


def load_policy(path: str | Path | None) -> Policy:
    """Load a policy TOML file.

    ``None`` or a nonexistent path returns the default policy; a file that
    exists but is malformed TOML or violates the schema raises PolicyError.
    """
    if path is None:
        return Policy()
    p = Path(path)
    if not p.is_file():
        return Policy()
    try:
        data = tomllib.loads(p.read_text())
    except tomllib.TOMLDecodeError as e:
        raise PolicyError(f"{p}: invalid TOML — {e}") from e
    try:
        return Policy.model_validate(data)
    except ValidationError as e:
        raise PolicyError(f"{p}: invalid policy — {e}") from e


def _suppressing_rule(flaw: Flaw, rules: list[SuppressRule]) -> SuppressRule | None:
    for rule in rules:
        if fnmatch.fnmatch(flaw.file, rule.glob) and (
            rule.match is None or rule.match.lower() in flaw.issue.lower()
        ):
            return rule
    return None


def apply_policy(verdict: Verdict, policy: Policy) -> Verdict:
    """Return a NEW Verdict with the policy applied.

    Order: severity_floor drops low-signal findings → suppressions remove
    accepted risks (counted in the summary for audit) → escalations bump
    remaining flaws in hot zones to high. Status is then recomputed:
    FAILED stays FAILED; breaking changes or any high/critical flaw fails;
    remaining flaws warn; a clean slate passes.
    """
    floor = _SEVERITY_ORDER[policy.severity_floor]
    kept: list[Flaw] = []
    suppressed = 0
    reasons: list[str] = []
    for flaw in verdict.critical_flaws:
        if _SEVERITY_ORDER[flaw.severity] < floor:
            continue
        rule = _suppressing_rule(flaw, policy.suppress)
        if rule is not None:
            suppressed += 1
            if rule.reason:
                reasons.append(rule.reason)
            continue
        if (_SEVERITY_ORDER[flaw.severity] < _SEVERITY_ORDER["high"]
                and any(fnmatch.fnmatch(flaw.file, e.glob) for e in policy.escalate)):
            flaw = flaw.model_copy(update={"severity": "high"})
        kept.append(flaw)

    if (verdict.status == "FAILED" or verdict.breaking_changes_detected
            or any(_SEVERITY_ORDER[f.severity] >= _SEVERITY_ORDER["high"] for f in kept)):
        status: Status = "FAILED"
    elif kept:
        status = "WARNING"
    else:
        status = "PASSED"

    summary = verdict.summary or ""
    if suppressed:
        note = f"{suppressed} finding{'s' if suppressed != 1 else ''} suppressed by policy"
        if reasons:
            note += f" ({'; '.join(reasons)})"
        summary = f"{summary} {note}".strip() if summary else note

    return verdict.model_copy(
        update={"status": status, "critical_flaws": kept, "summary": summary}
    )


def should_fail(verdict: Verdict, policy: Policy) -> bool:
    """True when the verdict's status is one of the policy's fail_on statuses."""
    return verdict.status in policy.fail_on
