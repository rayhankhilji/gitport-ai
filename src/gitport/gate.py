"""Step 3: strict quality gating via Cohere structured JSON outputs.

Forces the final judgment into a machine-readable schema so CI systems can
gate on it without parsing prose. On malformed output we retry once, then let
the engine decide (default: fail closed).
"""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from .agent import AgentResult
from .config import Settings
from .diff import diff_digest
from .models import FileDiff, RetrievedRule, Verdict

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["PASSED", "WARNING", "FAILED"]},
        "breaking_changes_detected": {"type": "boolean"},
        "critical_flaws": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "issue": {"type": "string"},
                    "fix_suggestion": {"type": "string"},
                    "severity": {
                        "type": "string",
                        "enum": ["low", "medium", "high", "critical"],
                    },
                },
                "required": ["file", "line", "issue"],
            },
        },
        "summary": {"type": "string"},
        "rules_applied": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Which internal rules were applied in the judgment",
        },
    },
    "required": ["status", "breaking_changes_detected", "critical_flaws"],
}

_SYSTEM = """\
You are gitport's final gate. Given a diff, the internal engineering rules that
apply to it, and the findings from real analysis tools, emit a verdict.

Rules of judgment:
- FAILED: logic errors, security flaws, or migration hazards that will break
  or endanger production. breaking_changes_detected must be true.
- WARNING: risky but not provably breaking — missing tests on critical paths,
  ambiguous API changes, things a human should glance at.
- PASSED: safe to merge.
- Only cite flaws you can point to in the diff or tool output. Do not invent
  issues. critical_flaws may be empty for PASSED.
- Line numbers must refer to the file's post-change line numbers shown in the
  diff digest."""


class GateError(RuntimeError):
    pass


def evaluate(client, cfg: Settings, files: list[FileDiff],
             rules: list[RetrievedRule], agent: AgentResult) -> Verdict:
    """Make the final schema-constrained cohere.chat call."""
    rules_block = "\n".join(
        f"- {r.source}: {r.heading or r.excerpt[:120]}" for r in rules
    ) or "<none retrieved>"
    findings = json.dumps(agent.transcript, default=str)[:20_000]

    user = (
        f"INTERNAL RULES APPLIED:\n{rules_block}\n\n"
        f"AGENT ANALYSIS NOTES:\n{agent.notes or '<none>'}\n\n"
        f"TOOL FINDINGS:\n{findings or '<no tools were run>'}\n\n"
        f"DIFF:\n{diff_digest(files, cfg.max_diff_chars)}\n\n"
        "Return the verdict as strict JSON matching the schema."
    )
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user},
    ]

    text = client.chat(
        model=cfg.chat_model,
        messages=messages,
        temperature=0,
        response_format={"type": "json_object", "schema": VERDICT_SCHEMA},
    ).message.content[0].text

    try:
        return _parse_verdict(text)
    except GateError:
        # one repair pass — some models wrap JSON in fences despite the schema
        text2 = client.chat(
            model=cfg.chat_model,
            messages=messages + [
                {"role": "assistant", "content": text},
                {"role": "user", "content":
                    "That was not valid JSON matching the schema. "
                    "Output only the JSON object."},
            ],
            temperature=0,
            response_format={"type": "json_object", "schema": VERDICT_SCHEMA},
        ).message.content[0].text
        return _parse_verdict(text2)


def _parse_verdict(text: str) -> Verdict:
    raw = _extract_json(text)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise GateError(f"verdict was not valid JSON: {e}") from e
    try:
        return Verdict.model_validate(data)
    except ValidationError as e:
        raise GateError(f"verdict did not match schema: {e}") from e


def _extract_json(text: str) -> str:
    """Tolerate markdown fences around the JSON object."""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if m:
        return m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text
