"""Enterprise output formats: JSON, Markdown, SARIF 2.1.0 and JUnit XML.

``render`` is the single entry point — the CLI, the GitHub comment poster and
CI integrations all share it so every surface emits the same report.
"""

from __future__ import annotations

import json
import re
from xml.sax.saxutils import escape, quoteattr

from . import __version__
from .models import CheckReport

_FORMATS = ("json", "markdown", "sarif", "junit")

_VERDICT_ICON = {"PASSED": "✅", "WARNING": "⚠️", "FAILED": "❌"}

# SARIF levels: error → fails the build, warning → suspicious, note → minor.
_SARIF_LEVEL = {"critical": "error", "high": "error", "medium": "warning", "low": "note"}


def render(report: CheckReport, fmt: str) -> str:
    """Render a CheckReport in one of: json, markdown, sarif, junit."""
    if fmt == "json":
        return report.model_dump_json(indent=2)
    if fmt == "markdown":
        return _markdown(report)
    if fmt == "sarif":
        return _sarif(report)
    if fmt == "junit":
        return _junit(report)
    raise ValueError(f"unknown format {fmt!r} — expected one of {', '.join(_FORMATS)}")


def _md_cell(text: str) -> str:
    """Escape a value so it stays inside one markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _markdown(report: CheckReport) -> str:
    """PR-comment-ready markdown: verdict, flaw table, collapsible audit trail."""
    v = report.verdict
    icon = _VERDICT_ICON.get(v.status, "")
    lines = [f"## {icon + ' ' if icon else ''}gitport: {v.status}", ""]
    if v.summary:
        lines += [v.summary, ""]

    if v.critical_flaws:
        lines.append("| file | line | severity | issue | fix |")
        lines.append("|------|-----:|----------|-------|-----|")
        for f in v.critical_flaws:
            lines.append(
                f"| {_md_cell(f.file)} | {f.line} | {f.severity} "
                f"| {_md_cell(f.issue)} | {_md_cell(f.fix_suggestion)} |"
            )
        lines.append("")

    if v.rules_applied:
        lines.append("**rules applied**")
        lines += [f"- {_md_cell(r)}" for r in v.rules_applied]
        lines.append("")

    # Audit trail — collapsed by default so the comment stays scannable.
    lines += ["<details><summary>agent notes & tool calls</summary>", ""]
    if report.agent_notes:
        lines += [report.agent_notes, ""]
    if report.tool_calls:
        lines.append("| tool | ok | summary |")
        lines.append("|------|----|---------|")
        for t in report.tool_calls:
            mark = "✅" if t.ok else "❌"
            lines.append(f"| {_md_cell(t.name)} | {mark} | {_md_cell(t.summary)} |")
        lines.append("")
    else:
        lines += ["_no tool calls recorded_", ""]
    lines += ["</details>", "", f"<sub>gitport {__version__} — {report.elapsed_seconds:.1f}s</sub>"]
    return "\n".join(lines)


def _slug(text: str) -> str:
    """Turn an issue description into a stable SARIF ruleId."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:80] or "finding"


def _sarif(report: CheckReport) -> str:
    """SARIF 2.1.0 document for code-scanning uploads (GitHub, Azure, etc.)."""
    rules: dict[str, dict] = {}
    results = []
    for f in report.verdict.critical_flaws:
        rule_id = _slug(f.issue)
        rules.setdefault(rule_id, {
            "id": rule_id,
            "shortDescription": {"text": f.issue},
        })
        message = f.issue
        if f.fix_suggestion:
            message = f"{f.issue} — suggested fix: {f.fix_suggestion}"
        results.append({
            "ruleId": rule_id,
            "level": _SARIF_LEVEL.get(f.severity, "warning"),
            "message": {"text": message},
            "locations": [{
                "physicalLocation": {
                    "artifactLocation": {"uri": f.file},
                    "region": {"startLine": max(1, f.line)},
                },
            }],
        })
    doc = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {
                "driver": {
                    "name": "gitport",
                    "version": __version__,
                    "informationUri": "https://github.com/rayhankhilji/gitport-ai",
                    "rules": list(rules.values()),
                },
            },
            "results": results,
        }],
    }
    return json.dumps(doc, indent=2)


def _junit(report: CheckReport) -> str:
    """JUnit XML: one testcase per flaw, plus a gitport-verdict testcase."""
    v = report.verdict
    cases = []
    failures = 0
    for f in v.critical_flaws:
        attrs = f"classname={quoteattr(f.file)} name={quoteattr(f.issue[:80])}"
        if f.severity in ("high", "critical"):
            failures += 1
            cases.append(
                f"<testcase {attrs}>"
                f"<failure message={quoteattr(f.issue)}>"
                f"{escape(f.fix_suggestion or f.issue)}</failure>"
                f"</testcase>"
            )
        else:
            cases.append(f"<testcase {attrs}/>")

    if v.status == "FAILED":
        failures += 1
        cases.append(
            '<testcase classname="gitport" name="gitport-verdict">'
            f'<failure message="verdict FAILED">{escape(v.summary or "merge blocked")}</failure>'
            "</testcase>"
        )
    else:
        cases.append('<testcase classname="gitport" name="gitport-verdict"/>')

    return "\n".join([
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<testsuite name="gitport" tests="{len(cases)}" failures="{failures}" '
        'errors="0" skipped="0">',
        *cases,
        "</testsuite>",
    ])
