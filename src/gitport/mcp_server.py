"""MCP server — exposes gitport as tools any MCP-aware agent can call.

Run over stdio (``gitport-mcp`` or ``gitport mcp``) and register it in an agent
client's MCP config. The pure-analysis tools work without a Cohere key; the
full check requires COHERE_API_KEY.
"""

from __future__ import annotations

import json
from pathlib import Path

try:  # mcp >= 2.0 renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer

from .analysis import analyze_python_source, lint_sql_migration
from .config import get_settings
from .detectors.secrets import scan_text_for_secrets
from .engine import EngineError, error_verdict, run_check
from .models import CheckReport
from .policy import find_policy_path, load_policy

mcp = MCPServer(
    "gitport",
    instructions=(
        "gitport is a pre-merge gatekeeper. Call gitport_check (git refs) or "
        "gitport_check_diff (raw unified diff) before merging code. The verdict "
        "JSON has status PASSED/WARNING/FAILED plus critical_flaws. FAILED or "
        "breaking_changes_detected=true means do not merge."
    ),
)


def _settings():
    return get_settings()


def _report_json(report: CheckReport) -> str:
    return report.model_dump_json()


@mcp.tool()
def gitport_check(repo_path: str, base_ref: str = "HEAD~1", head_ref: str = "HEAD") -> str:
    """Gate a git range in a local repository. Applies .gitport/policy.toml
    from the repo when present. Returns the verdict JSON."""
    cfg = _settings()
    pol = load_policy(p) if (p := find_policy_path(cfg, repo_path)) else None
    try:
        report = run_check(cfg, repo=repo_path, base=base_ref,
                           head=head_ref, policy=pol)
    except EngineError as e:
        report = CheckReport(verdict=error_verdict(str(e), cfg.fail_open))
    except Exception as e:
        report = CheckReport(verdict=error_verdict(str(e), cfg.fail_open))
    return _report_json(report)


@mcp.tool()
def gitport_check_diff(diff_text: str) -> str:
    """Gate a raw unified diff (e.g. a pull request patch). Returns verdict JSON."""
    cfg = _settings()
    try:
        report = run_check(cfg, diff_text=diff_text)
    except EngineError as e:
        report = CheckReport(verdict=error_verdict(str(e), cfg.fail_open))
    except Exception as e:
        report = CheckReport(verdict=error_verdict(str(e), cfg.fail_open))
    return _report_json(report)


@mcp.tool()
def gitport_lint_migration(sql: str) -> str:
    """Lint SQL migration text for table-locking hazards. No API key needed."""
    return json.dumps(lint_sql_migration(sql))


@mcp.tool()
def gitport_analyze_python(source: str, filename: str = "snippet.py") -> str:
    """AST-parse Python source for dangerous calls. No API key needed."""
    return json.dumps(analyze_python_source(source, filename))


@mcp.tool()
def gitport_scan_secrets(text: str, filename: str = "snippet") -> str:
    """Scan text for leaked credentials (keys, tokens, JWTs). No API key
    needed; matches are masked in the output."""
    return json.dumps(scan_text_for_secrets(text, filename))


@mcp.tool()
def gitport_index_rules(rules_dir: str, index_path: str = ".gitport/index.sqlite3") -> str:
    """Embed a directory of internal engineering docs into the vector index."""
    from .engine import make_client
    from .rules import IndexError_, build_index

    cfg = _settings()
    try:
        n = build_index(make_client(cfg), cfg, Path(rules_dir), Path(index_path))
    except (EngineError, IndexError_) as e:
        return json.dumps({"ok": False, "error": str(e)})
    return json.dumps({"ok": True, "chunks": n, "index": index_path})


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
