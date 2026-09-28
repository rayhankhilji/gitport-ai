"""gitport CLI — check diffs, index rules, install hooks, serve the API."""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .config import get_settings
from .engine import EngineError, error_verdict, run_check
from .models import CheckReport

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

    try:
        report = run_check(cfg, repo=repo, **kwargs)
    except EngineError as e:
        report = CheckReport(verdict=error_verdict(str(e), fail_open=fo))
    except Exception as e:  # upstream API failure, malformed diff, etc.
        report = CheckReport(verdict=error_verdict(f"{type(e).__name__}: {e}", fail_open=fo))

    if json_out:
        console.print_json(report.model_dump_json())
    elif quiet:
        console.print(report.verdict.status)
    else:
        _render(report)

    status = report.verdict.status
    if status == "FAILED" or (strict and status == "WARNING"):
        raise typer.Exit(EXIT_FAILED)
    if report.verdict.error and not fo:
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
    "pre-push": """#!/bin/sh
# gitport pre-push gate: check each ref range being pushed.
remote="$1"
status=0
while read local_ref local_sha remote_ref remote_sha; do
    if [ "$remote_sha" = "0000000000000000000000000000000000000000" ]; then
        range="$local_sha"          # new branch: diff against itself's commits
    else
        range="$remote_sha...$local_sha"
    fi
    gitport check --base "$range" || status=1
done
exit $status
""",
    "pre-receive": """#!/bin/sh
# gitport pre-receive gate (server-side). Install in the bare repo's hooks/.
status=0
while read old_sha new_sha ref; do
    if [ "$old_sha" = "0000000000000000000000000000000000000000" ]; then
        range="$new_sha"
    else
        range="$old_sha...$new_sha"
    fi
    gitport check --base "$range" || status=1
done
exit $status
""",
}


def main() -> None:  # console_scripts entry
    app()


if __name__ == "__main__":
    main()
