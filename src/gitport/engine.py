"""End-to-end pipeline: diff → rules → agent → verdict.

``run_check`` is the single entry point shared by the CLI, the MCP server and
the REST API. The Cohere client is injectable so tests (and users) can swap in
a stub without network access.
"""

from __future__ import annotations

import time
from pathlib import Path

from .agent import run_agent
from .config import Settings
from .diff import GitError, diff_digest, git_diff, parse_unified_diff
from .gate import evaluate
from .models import CheckReport, RetrievedRule, Verdict
from .policy import Policy, apply_policy
from .rules import retrieve_rules


class EngineError(RuntimeError):
    pass


def make_client(cfg: Settings):
    """Build a real Cohere ClientV2. Raises EngineError if unconfigured."""
    if not cfg.cohere_api_key:
        raise EngineError(
            "COHERE_API_KEY is not set — add it to .env or the environment. "
            "Get a key at https://dashboard.cohere.com/api-keys"
        )
    import cohere
    return cohere.ClientV2(api_key=cfg.cohere_api_key)


def run_check(cfg: Settings, client=None, *,
              diff_text: str | None = None,
              repo: str | Path = ".",
              base: str | None = None,
              head: str | None = None,
              staged: bool = False,
              policy: Policy | None = None) -> CheckReport:
    """Run the full gatekeeper pipeline and return a CheckReport.

    Exactly one diff source is used: ``diff_text`` if given, otherwise a git
    diff over ``base...head`` (or staged changes when ``staged=True``).
    When ``policy`` is provided, it is applied to the verdict before return —
    the same enforcement for CLI, API and MCP callers.
    """
    t0 = time.monotonic()

    if diff_text is None:
        try:
            diff_text = git_diff(repo, base=base, head=head, staged=staged)
        except GitError as e:
            raise EngineError(str(e)) from e

    files = parse_unified_diff(diff_text)
    stats = dict(
        files_changed=len(files),
        lines_added=sum(len(f.added) for f in files),
    )
    if not files:
        return CheckReport(
            verdict=Verdict(status="PASSED", breaking_changes_detected=False,
                            summary="Empty diff — nothing to gate."),
            elapsed_seconds=time.monotonic() - t0,
            **stats,
        )

    if client is None:
        client = make_client(cfg)

    digest = diff_digest(files, cfg.max_diff_chars)

    # Step 1: retrieval. A missing index degrades gracefully — the gate still
    # runs on tool findings, it just can't cite internal rules.
    rules: list[RetrievedRule] = []
    try:
        rules = retrieve_rules(client, cfg, digest)
    except Exception:
        rules = []

    # Step 2: agent tool-use loop. The agent may read full files from the repo
    # working tree for context even when the diff came in as text.
    agent = run_agent(client, cfg, files, rules,
                      repo_root=repo if Path(repo).is_dir() else None)

    # Step 3: structured verdict, then policy enforcement (suppressions,
    # severity floors, escalation zones) unless the gate itself errored.
    verdict = evaluate(client, cfg, files, rules, agent)
    if policy is not None and verdict.error is None:
        verdict = apply_policy(verdict, policy)

    return CheckReport(
        verdict=verdict,
        rules=rules,
        tool_calls=agent.records,
        agent_notes=agent.notes,
        elapsed_seconds=time.monotonic() - t0,
        **stats,
    )


def error_verdict(message: str, fail_open: bool = False) -> Verdict:
    """Verdict for engine-level failures. Default is to fail closed."""
    if fail_open:
        return Verdict(
            status="WARNING",
            breaking_changes_detected=False,
            summary=f"gitport could not evaluate this change: {message}",
            error=message,
        )
    return Verdict(
        status="FAILED",
        breaking_changes_detected=False,
        summary=f"gitport could not evaluate this change (failing closed): {message}",
        error=message,
    )
