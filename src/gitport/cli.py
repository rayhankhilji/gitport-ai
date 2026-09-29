"""gitport CLI — check diffs, index rules, install hooks, serve the API."""

from __future__ import annotations

import sys
from enum import StrEnum
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .config import get_settings
from .engine import EngineError, error_verdict, run_check
from .formatters import render
from .github import post_pr_comment, set_commit_status
from .models import CheckReport
from .policy import PolicyError, find_policy_path, load_policy, should_fail

app = typer.Typer(
    name="gitport",
    help="AI-native pre-merge gatekeeper: blocks breaking changes before they hit production.",
    no_args_is_help=True,
)
console = Console()
err_console = Console(stderr=True)

EXIT_PASSED = 0
EXIT_FAILED = 1
EXIT_ERROR = 3

_STATUS_STYLE = {"PASSED": "green", "WARNING": "yellow", "FAILED": "red"}


class _Format(StrEnum):
    rich = "rich"
    json = "json"
    markdown = "markdown"
    sarif = "sarif"
    junit = "junit"


@app.command()
def check(
    base: str | None = typer.Option(None, "--base", help="Base git ref (e.g. main, HEAD~1)"),
    head: str | None = typer.Option(None, "--head", help="Head git ref (default: working tree/HEAD)"),
    staged: bool = typer.Option(False, "--staged", help="Check staged changes"),
    diff_file: Path | None = typer.Option(None, "--diff-file", "-f",
                                             help="Read a unified diff from a file ('-' for stdin)"),
    repo: Path = typer.Option(Path("."), "--repo", help="Repository root"),
    strict: bool = typer.Option(False, "--strict", help="Treat WARNING as a failure"),
    fail_open: bool | None = typer.Option(
        None, "--fail-open/--fail-closed",
        help="On engine error, warn instead of failing (default: fail closed)"),
    fmt: _Format = typer.Option(
        _Format.rich, "--format", help="Output format (json/markdown/sarif/junit for CI)"),
    policy: Path | None = typer.Option(
        None, "--policy", help="Policy TOML file (default: .gitport/policy.toml when present)"),
    post_comment: bool = typer.Option(
        False, "--post-comment", help="Post a markdown report comment on a GitHub PR"),
    pr: int | None = typer.Option(None, "--pr", help="Pull request number for --post-comment"),
    repo_slug: str | None = typer.Option(
        None, "--repo-slug", help="GitHub repo as owner/name (for --post-comment/--set-status)"),
    set_status: bool = typer.Option(
        False, "--set-status", help="Set a GitHub commit status for this check"),
    sha: str | None = typer.Option(None, "--sha", help="Commit SHA for --set-status"),
    json_out: bool = typer.Option(False, "--json", help="Emit the full report as JSON"),
    quiet: bool = typer.Option(False, "-q", "--quiet", help="Only print the verdict line"),
) -> None:
    """Gate a diff: retrieve rules, run analysis tools, emit a verdict."""
    cfg = get_settings()
    fo = cfg.fail_open if fail_open is None else fail_open

    if diff_file is not None:
        diff_text = sys.stdin.read() if str(diff_file) == "-" else diff_file.read_text()
        kwargs = {"diff_text": diff_text}
    else:
        kwargs = {"base": base, "head": head, "staged": staged}

    # Policy-as-code: an explicit --policy wins, else .gitport/policy.toml
    # under the repo root is picked up automatically when it exists.
    policy_path = policy or find_policy_path(cfg, repo)
    if policy and not Path(policy).is_file():
        err_console.print(f"[red]policy file not found:[/red] {policy}")
        raise typer.Exit(EXIT_ERROR)
    try:
        pol = load_policy(policy_path)
    except PolicyError as e:
        err_console.print(f"[red]invalid policy file:[/red] {e}")
        raise typer.Exit(EXIT_ERROR) from e

    try:
        report = run_check(cfg, repo=repo,
                           policy=pol if policy_path else None, **kwargs)
    except EngineError as e:
        report = CheckReport(verdict=error_verdict(str(e), fail_open=fo))
    except Exception as e:  # upstream API failure, malformed diff, etc.
        report = CheckReport(verdict=error_verdict(f"{type(e).__name__}: {e}", fail_open=fo))

    # GitHub side-effects are best-effort: failures warn but never gate.
    if post_comment:
        if pr is None or not repo_slug:
            err_console.print(
                "[yellow]--post-comment needs --pr and --repo-slug; skipping[/yellow]")
        else:
            try:
                post_pr_comment(report, repo_slug, pr)
                err_console.print(f"[dim]posted gitport comment on PR #{pr}[/dim]")
            except Exception as e:
                err_console.print(f"[yellow]warning: could not post PR comment: {e}[/yellow]")
    if set_status:
        if not sha or not repo_slug:
            err_console.print(
                "[yellow]--set-status needs --sha and --repo-slug; skipping[/yellow]")
        else:
            try:
                set_commit_status(report, repo_slug, sha)
                err_console.print(f"[dim]set gitport status on {sha[:12]}[/dim]")
            except Exception as e:
                err_console.print(f"[yellow]warning: could not set commit status: {e}[/yellow]")

    out_fmt = "json" if json_out else fmt.value
    if out_fmt == "rich":
        if quiet:
            console.print(report.verdict.status)
        else:
            _render(report)
    else:
        print(render(report, out_fmt))

    v = report.verdict
    if should_fail(v, pol) or (strict and v.status == "WARNING"):
        raise typer.Exit(EXIT_FAILED)
    if v.error and not fo:
        raise typer.Exit(EXIT_ERROR)
    raise typer.Exit(EXIT_PASSED)


@app.command()
def index(
    rules_dir: Path | None = typer.Option(None, "--rules-dir", "-d",
                                           help="Directory of internal docs to index"),
    index_path: Path | None = typer.Option(None, "--index", "-o",
                                              help="Where to write the vector index"),
    repo: Path = typer.Option(Path("."), "--repo", help="Repository root"),
) -> None:
    """Embed the repo's internal docs (design rules, runbooks, post-mortems)."""
    from .rules import IndexError_, build_index

    cfg = get_settings()
    rules_dir = rules_dir or Path(cfg.rules_dir)
    index_path = index_path or Path(cfg.index_path)
    if not rules_dir.is_absolute():
        rules_dir = repo / rules_dir
    if not index_path.is_absolute():
        index_path = repo / index_path

    try:
        from .engine import make_client
        n = build_index(make_client(cfg), cfg, rules_dir, index_path)
    except (EngineError, IndexError_) as e:
        err_console.print(f"[red]index failed:[/red] {e}")
        raise typer.Exit(EXIT_ERROR) from e

    console.print(f"[green]indexed {n} chunks[/green] from {rules_dir} → {index_path}")


@app.command("install-hook")
def install_hook(
    hook_type: str = typer.Argument("pre-push", help="Hook to install (pre-push or pre-receive)"),
    repo: Path = typer.Option(Path("."), "--repo", help="Repository root"),
) -> None:
    """Install a git hook that runs `gitport check` before code lands."""
    if hook_type not in ("pre-push", "pre-receive"):
        err_console.print("[red]supported hooks: pre-push, pre-receive[/red]")
        raise typer.Exit(EXIT_ERROR)

    hooks_dir = repo / ".git" / "hooks"
    if not hooks_dir.is_dir():
        hooks_dir = repo / "hooks"  # bare repo layout
    if not hooks_dir.is_dir():
        err_console.print(f"[red]not a git repo:[/red] {repo}")
        raise typer.Exit(EXIT_ERROR)

    script = _HOOK_SCRIPTS[hook_type]
    dest = hooks_dir / hook_type
    dest.write_text(script)
    dest.chmod(0o755)
    console.print(f"[green]installed {hook_type} hook[/green] → {dest}")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8400, "--port"),
    repo: Path = typer.Option(Path("."), "--repo", help="Default repository root for checks"),
) -> None:
    """Run the gitport REST API (FastAPI + uvicorn)."""
    import uvicorn

    from .api import create_app

    uvicorn.run(create_app(default_repo=repo), host=host, port=port)


@app.command()
def mcp() -> None:
    """Run the MCP server over stdio (for agent clients like Devin/Cursor)."""
    from .mcp_server import main
    main()


@app.command()
def version() -> None:
    """Print the gitport version."""
    console.print(f"gitport {__version__}")


def _render(report: CheckReport) -> None:
    v = report.verdict
    style = _STATUS_STYLE.get(v.status, "white")
    console.print(Panel(
        f"[bold {style}]{v.status}[/bold {style}]  {v.summary or ''}",
        title="gitport verdict",
        subtitle=f"{report.files_changed} files, +{report.lines_added} lines, "
                 f"{len(report.tool_calls)} tool calls, {report.elapsed_seconds:.1f}s",
    ))

    if v.critical_flaws:
        table = Table(title="critical flaws")
        table.add_column("file", style="cyan")
        table.add_column("line", justify="right")
        table.add_column("severity")
        table.add_column("issue")
        table.add_column("suggested fix", style="dim")
        for f in v.critical_flaws:
            table.add_row(f.file, str(f.line), f.severity, f.issue, f.fix_suggestion)
        console.print(table)

    if report.rules:
        console.print("[dim]rules applied: "
                      + ", ".join(r.source for r in report.rules) + "[/dim]")


_HOOK_SCRIPTS = {
    # EMPTY is the well-known empty-tree object hash — used as the base when a
    # push creates a ref, so the whole branch's diff gets checked.
    "pre-push": """#!/bin/sh
# gitport pre-push gate: check each ref range being pushed.
# Requires: gitport on PATH and COHERE_API_KEY exported.
remote="$1"
EMPTY=4b825dc642cb6eb9a060e54bf8d69288fbee4904
status=0
while read local_ref local_sha remote_ref remote_sha; do
    if [ "$local_sha" = "0000000000000000000000000000000000000000" ]; then
        continue                        # branch deletion — nothing to gate
    fi
    if [ "$remote_sha" = "0000000000000000000000000000000000000000" ]; then
        gitport check --base "$EMPTY" --head "$local_sha" || status=1
    else
        gitport check --base "$remote_sha" --head "$local_sha" || status=1
    fi
done
exit $status
""",
    "pre-receive": """#!/bin/sh
# gitport pre-receive gate (server-side). Install in the bare repo's hooks/.
# Requires: gitport on PATH and COHERE_API_KEY exported in the hook env.
EMPTY=4b825dc642cb6eb9a060e54bf8d69288fbee4904
status=0
while read old_sha new_sha ref; do
    if [ "$new_sha" = "0000000000000000000000000000000000000000" ]; then
        continue                        # ref deletion — nothing to gate
    fi
    if [ "$old_sha" = "0000000000000000000000000000000000000000" ]; then
        gitport check --base "$EMPTY" --head "$new_sha" || status=1
    else
        gitport check --base "$old_sha" --head "$new_sha" || status=1
    fi
done
exit $status
""",
}


def main() -> None:  # console_scripts entry
    app()


if __name__ == "__main__":
    main()
