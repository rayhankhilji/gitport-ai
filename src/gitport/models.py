"""Pydantic models shared across the CLI, MCP server and REST API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AddedLine(BaseModel):
    """A single line added by a diff, tracked against its new-file line number."""

    lineno: int
    text: str


class FileDiff(BaseModel):
    """One file's worth of changes from a unified diff."""

    path: str
    old_path: str | None = None
    is_new: bool = False
    is_deleted: bool = False
    is_binary: bool = False
    added: list[AddedLine] = Field(default_factory=list)
    deleted_count: int = 0

    @property
    def added_source(self) -> str:
        """Reconstructed text of only the added lines (fragmentary)."""
        return "\n".join(line.text for line in self.added)


class RetrievedRule(BaseModel):
    """An internal engineering rule retrieved for this diff."""

    source: str
    heading: str = ""
    excerpt: str
    score: float = 0.0


class ToolCallRecord(BaseModel):
    """Audit trail of one tool call made by the agent loop."""

    name: str
    arguments: dict = Field(default_factory=dict)
    ok: bool = True
    summary: str = ""


class Flaw(BaseModel):
    """A single critical flaw found in the diff."""

    file: str
    line: int = 0
    issue: str
    fix_suggestion: str = ""
    severity: Literal["low", "medium", "high", "critical"] = "high"


class Verdict(BaseModel):
    """The structured gatekeeper verdict. Mirrors the Cohere response_format schema."""

    status: Literal["PASSED", "WARNING", "FAILED"]
    breaking_changes_detected: bool
    critical_flaws: list[Flaw] = Field(default_factory=list)
    summary: str = ""
    rules_applied: list[str] = Field(default_factory=list)
    error: str | None = None


class CheckReport(BaseModel):
    """Full result of a gitport check run: verdict plus supporting evidence."""

    verdict: Verdict
    rules: list[RetrievedRule] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    agent_notes: str = ""
    files_changed: int = 0
    lines_added: int = 0
    elapsed_seconds: float = 0.0
