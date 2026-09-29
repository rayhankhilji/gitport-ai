# Deployment

Five ways to run gitport on the merge path. All of them need
`COHERE_API_KEY`; everything else is optional tuning via `GITPORT_*` env vars.

## (a) GitHub Action

[`examples/github-action.yml`](../examples/github-action.yml) gates every PR:

```yaml
name: gitport
on:
  pull_request:                       # run on every PR event (open, sync, reopen)

jobs:
  gate:
    runs-on: ubuntu-latest
    permissions:
      contents: read                  # checkout only
      pull-requests: write            # reserved for PR comments/annotations
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0              # full history — merge-base diff needs it

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - run: pip install gitport      # or "git+https://github.com/rayhankhilji/gitport-ai"

      - name: Index internal rules
        run: gitport index --rules-dir docs/rules
        env:
          COHERE_API_KEY: ${{ secrets.COHERE_API_KEY }}

      - name: Gate the PR diff
        env:
          COHERE_API_KEY: ${{ secrets.COHERE_API_KEY }}
        run: gitport check --base origin/${{ github.base_ref }} --head HEAD --strict
```

Line-by-line decisions that matter:

- `fetch-depth: 0` — `gitport check --base origin/main` resolves a merge
  base; a shallow clone can produce a wrong or empty diff.
- `github.base_ref` — the PR's target branch, so the same workflow works for
  `main`, release branches, whatever the PR targets.
- `--strict` — WARNING also blocks. Drop it if warnings should inform but
  not gate.
- `COHERE_API_KEY` comes from repo secrets; the step fails closed if it's
  missing — an un-gated merge is the dangerous state, not a red job.

Optional extras for the gate step:

```bash
# SARIF for GitHub code scanning, a markdown PR comment, a commit status:
gitport check --base origin/${{ github.base_ref }} --head HEAD --strict \
  --format sarif > gitport.sarif          # then upload-sarif it
gitport check ... --format markdown      # PR-comment-ready body
gitport check ... --post-comment --pr ${{ github.event.number }} \
  --repo-slug ${{ github.repository }}   # upserts one marked comment
gitport check ... --set-status --sha ${{ github.event.pull_request.head.sha }} \
  --repo-slug ${{ github.repository }}   # success/failure commit status
```

`--post-comment`/`--set-status` need `GITHUB_TOKEN` (the workflow token is
enough — `pull-requests: write` / `statuses: write` permissions) and warn
instead of failing if the API call breaks.

GitLab equivalent: [`examples/gitlab-ci.yml`](../examples/gitlab-ci.yml)
(same two steps, `origin/$CI_MERGE_REQUEST_TARGET_BRANCH_NAME` as base).

## (b) Git server `pre-receive` hook

For self-hosted git (bare repo on a server, Gitea/GitLab-CE SSH, etc.):

```bash
# in the bare repo
gitport install-hook pre-receive --repo /srv/repo.git
# writes + chmod 755 /srv/repo.git/hooks/pre-receive
```

The installed script reads `old_sha new_sha ref` lines from stdin and runs
`gitport check --base $old_sha --head $new_sha` per updated ref — deletions
are skipped, brand-new refs diff against git's empty tree
(`4b825dc…`). Any `FAILED` check sets exit 1 and the whole push is rejected.

**Environment requirements.** Server hooks run with a minimal env, so both
must hold for the git user:

- `gitport` on `PATH` (`pipx install`, a venv on PATH, or a full path —
  edit the script if needed)
- `COHERE_API_KEY` exported — persist it in the hook script, a wrapper,
  or the service manager's environment, e.g. prepend
  `export COHERE_API_KEY=...` to `hooks/pre-receive`.

**Fail semantics.** Default is fail-closed: if Cohere is unreachable or the
key is missing, checks report `FAILED` and pushes are rejected. For a gate
that degrades instead, export `GITPORT_FAIL_OPEN=1` in the hook env — engine
errors become WARNINGs and the push proceeds flagged. Every pushed ref is
checked (one Cohere run each); on busy servers consider wrapping the script
to gate only protected refs.

## (c) Self-hosted API (docker compose)

`docker-compose.yml` runs the FastAPI service:

```bash
cp .env.example .env
# .env: COHERE_API_KEY=... and GITPORT_API_TOKEN=<long random string>
docker compose up --build -d
curl localhost:8400/healthz        # {"status":"ok","version":"0.1.0"}
```

- **Service** `api`: image built from the repo `Dockerfile` (multi-stage,
  non-root `gitport` user), port `8400`, `restart: unless-stopped`.
- **State**: named volume `gitport-data` at `/data` —
  `GITPORT_INDEX_PATH=/data/index.sqlite3` (vector index) and
  `GITPORT_REPORTS_DB=/data/reports.sqlite3` survive rebuilds. Your rules
  corpus mounts read-only at `/rules` (`GITPORT_RULES_DIR=/rules`).
- **Auth**: with `GITPORT_API_TOKEN` set, every `/v1/*` call needs
  `Authorization: Bearer <token>`; `/healthz` is open for probes. The
  healthcheck uses `python -c urllib…` — no curl in the slim image.
- **Hardening knobs** (env): `GITPORT_RATE_LIMIT_RPM` (default 120, per
  client IP, `/healthz` exempt), `GITPORT_MAX_REQUEST_BYTES` (default 5 MB →
  413), `GITPORT_CORS_ORIGINS` (comma-separated allowlist; unset = no CORS),
  `GITPORT_LOG_FORMAT=json` for structured logs. Every response carries an
  `X-Request-ID`; errors share the `{"error": {type, message, request_id}}`
  envelope.
- **Audit**: each `/v1/check` is persisted (`GITPORT_REPORTS_DB`,
  `GITPORT_STORE_REPORTS=0` to disable) and returns an `id`; query
  `GET /v1/checks`, `GET /v1/checks/{id}`, `GET /v1/stats`.
- **(Re)index after changing rules**: `docker compose exec api gitport index
  --rules-dir /rules` or `POST /v1/index` (authed).
- **TLS**: uvicorn serves plain HTTP — put the container behind a reverse
  proxy (Caddy, nginx, Traefik) for TLS and only publish the port on the
  proxy's network. Without `GITPORT_API_TOKEN` treat the port as
  localhost-only. The rate limiter is per-worker in-memory — enforce limits
  at the proxy if you run replicas.

```bash
curl -X POST https://gitport.internal/v1/check \
  -H "authorization: Bearer $GITPORT_API_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"diff": "<unified diff>"}'   # or {"repo_path","base_ref","head_ref"}
```

## (d) MCP client config

`gitport-mcp` (or `gitport mcp`) speaks MCP over stdio. Same block in every
client — [`examples/mcp-config.json`](../examples/mcp-config.json):

```json
{
  "mcpServers": {
    "gitport": {
      "command": "gitport-mcp",
      "env": { "COHERE_API_KEY": "your-key-here" }
    }
  }
}
```

| Client | Where the JSON goes |
|---|---|
| Claude Desktop | `claude_desktop_config.json` (`~/Library/Application Support/Claude/` on macOS, `%APPDATA%\Claude\` on Windows) |
| Cursor | `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (per-project) |
| Devin | MCP servers settings — paste the `mcpServers` block |

Tools exposed: `gitport_check` (repo_path + base/head refs),
`gitport_check_diff` (raw unified diff), `gitport_index_rules`, plus keyless
`gitport_lint_migration`, `gitport_analyze_python`, `gitport_scan_secrets`.
The full check needs `COHERE_API_KEY`; the three lint tools work without it.

## (e) Policy file

Policy-as-code, implemented in `policy.py` and applied inside
`engine.run_check` (so every surface gets the same enforcement). `gitport
check` auto-loads `<repo>/.gitport/policy.toml` when the file exists;
`--policy PATH` overrides, and `GITPORT_POLICY_PATH` changes the default
location. A malformed file — or a `--policy` path that doesn't exist — is a
hard error (exit 3): a policy that doesn't load must not silently pass.
See [`examples/policy.toml`](../examples/policy.toml):

```toml
severity_floor = "low"            # low | medium | high | critical
fail_on = ["FAILED"]              # verdict statuses that block the merge

[[suppress]]                      # known-risk acceptances (audited, not hidden)
glob = "vendor/**"                # fnmatch on the flaw's file — required
match = "subprocess"              # optional substring filter on the issue text
reason = "vendored code is upstream's problem"   # recorded for audit

[[escalate]]                      # hot zones
glob = "migrations/**"            # sub-"high" flaws on matching paths bump to high
```

| Key | Type | Meaning |
|---|---|---|
| `severity_floor` | `low` \| `medium` \| `high` \| `critical` (default `low`) | flaws below this severity are dropped before status is computed |
| `fail_on` | list of `PASSED` \| `WARNING` \| `FAILED` (default `["FAILED"]`) | verdict statuses that exit 1 — the list is authoritative: `["FAILED", "WARNING"]` gates on warnings without needing `--strict` |
| `[[suppress]]` | `{glob, match?, reason?}` | matching flaws are removed and counted in the summary (`"2 findings suppressed by policy (…)"`) for audit |
| `[[escalate]]` | `{glob}` | flaws below `high` on matching paths are bumped to `high` |

Application order in `apply_policy`: floor → suppress → escalate, then the
status is recomputed: `FAILED` stays `FAILED`; `breaking_changes_detected`
or any remaining `high`/`critical` flaw → `FAILED`; remaining flaws →
`WARNING`; clean slate → `PASSED`. `should_fail` then maps status → exit
code via `fail_on`. No file = defaults (no floor, fail on `FAILED`).
