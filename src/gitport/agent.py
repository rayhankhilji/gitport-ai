"""Step 2 of the pipeline: multi-step tool execution via ``cohere.chat``.

The model acts as an agent — it inspects the diff, calls the analysis tools it
judges relevant (AST parsing, migration lint, dependency scan, file reads),
receives real results, and produces analysis notes. The loop runs until the
model stops calling tools or the step budget is exhausted.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import Settings
from .diff import diff_digest
from .models import FileDiff, RetrievedRule, ToolCallRecord
from .tools import TOOL_SCHEMAS, ToolContext, execute_tool, record, tool_message_content

_SYSTEM = """\
You are gitport, a production pre-merge gatekeeper for code changes.

You are reviewing a git diff before it is allowed to merge. You have tools to
run real checks — use them instead of guessing:

- For changed .py files: call analyze_python_file to get its structure and
  dangerous call sites.
- For changed .sql files or migration directories: call lint_migration_file to
  catch operations that lock or destroy production tables.
- For changed dependency manifests: call scan_manifest to check for known
  CVEs.
- For every changed file: call scan_secrets to catch leaked credentials.
- For changed JS/TS/Go/Java/Ruby/PHP files: call scan_source_patterns.
- For changed Dockerfiles or CI workflow files: call lint_config_file.
- Call read_file when you need surrounding context the diff doesn't show.

Also weigh the diff against the internal engineering rules provided. Only call
tools for files actually present in the diff. When you have enough evidence,
stop calling tools and write concise analysis notes: what changed, which rules
apply, what the checks found, and anything the tools could not cover."""


def _rules_block(rules: list[RetrievedRule]) -> str:
    if not rules:
        return "<no internal rules indexed>"
    lines = []
    for i, r in enumerate(rules, 1):
        label = f"{r.source}" + (f" — {r.heading}" if r.heading else "")
        lines.append(f"[RULE {i}] {label} (relevance {r.score:.2f})\n{r.excerpt}")
    return "\n\n".join(lines)


class AgentResult:
    def __init__(self, notes: str, records: list[ToolCallRecord], transcript: list[dict]):
        self.notes = notes
        self.records = records
        self.transcript = transcript


def run_agent(client, cfg: Settings, files: list[FileDiff],
              rules: list[RetrievedRule], repo_root: str | Path | None = None) -> AgentResult:
    """Drive the cohere.chat tool-use loop until the model finishes."""
    ctx = ToolContext(
        file_diffs={f.path: f for f in files},
        repo_root=Path(repo_root) if repo_root else None,
    )
    user = (
        "Review this diff.\n\n"
        f"INTERNAL ENGINEERING RULES (retrieved for this diff):\n{_rules_block(rules)}\n\n"
        f"DIFF:\n{diff_digest(files, cfg.max_diff_chars)}"
    )
    messages: list[dict] = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user},
    ]

    records: list[ToolCallRecord] = []
    transcript: list[dict] = []

    for _ in range(cfg.agent_max_steps):
        response = client.chat(
            model=cfg.chat_model,
            messages=messages,
            tools=TOOL_SCHEMAS,
            temperature=cfg.agent_temperature,
        )
        msg = response.message
        calls = getattr(msg, "tool_calls", None)
        if not calls:
            notes = ""
            if getattr(msg, "content", None):
                notes = msg.content[0].text or ""
            return AgentResult(notes=notes, records=records, transcript=transcript)

        messages.append(msg)
        for tc in calls:
            args = json.loads(tc.function.arguments or "{}")
            result = execute_tool(ctx, tc.function.name, args)
            records.append(record(tc.function.name, args, result))
            transcript.append({
                "tool": tc.function.name,
                "arguments": args,
                "result": result,
            })
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": tool_message_content(result),
            })

    # budget exhausted — take whatever the model last said
    notes = ""
    if getattr(messages[-1], "content", None):
        try:
            notes = messages[-1].content[0].text
        except Exception:
            notes = ""
    return AgentResult(notes=notes or "(agent step budget exhausted)",
                       records=records, transcript=transcript)
