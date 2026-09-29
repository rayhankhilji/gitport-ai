# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-09-29

First public release. `gitport` gates a git diff before it merges: embed your
internal rules, run deterministic checks through a Cohere tool-use agent
loop, emit a schema-constrained `PASSED` / `WARNING` / `FAILED` verdict.

### Added

**Engine** (`diff` → `rules` → `agent` → `gate`, orchestrated in `engine.py`)

- Unified-diff ingestion from git ranges (`base...head`), staged changes, or
  raw diff text; per-file parsing with post-change line tracking and binary /
  new / deleted / renamed handling (`diff.py`).
- Two-stage rules retrieval: `cohere.embed` (batch ≤ 96, `search_document` /
  `search_query`) into a SQLite vector store, cosine candidates reranked by
  `cohere.rerank` to top-K (`rules.py`, `store.py`). Heading-aware chunking
  with overlap (`GITPORT_CHUNK_SIZE` / `GITPORT_CHUNK_OVERLAP`).
- Agent tool-use loop over `cohere.chat` with a configurable step budget
  (`GITPORT_AGENT_MAX_STEPS`, default 8) and tool results returned as Cohere
  document blocks for citations (`agent.py`, `tools.py`).
- Structured verdict via `cohere.chat` +
  `response_format={"type": "json_object", "schema": VERDICT_SCHEMA}` with
  one JSON-repair retry (`gate.py`). Verdict contract: `status`,
  `breaking_changes_detected`, `critical_flaws[]` (file, line, issue,
  fix_suggestion, severity), `summary`, `rules_applied`.
- Fail-closed error handling: engine errors produce `FAILED` verdicts;
  `GITPORT_FAIL_OPEN=1` / `--fail-open` downgrades to `WARNING`.

**Deterministic checks** (invoked as agent tools, never guessed)

- Python AST analysis: structure report plus dangerous-call detection —
  `eval`, `exec`, `compile`, `__import__`, `os.system`, `os.popen`,
  `pickle.load(s)`, `marshal.loads`, `yaml.load`, `hashlib.md5`/`sha1`,
  `tempfile.mktemp`, `subprocess.*(shell=True)` (`analysis.py`).
- SQL migration lint: ten hazard rules (`DROP_TABLE`, `DROP_COLUMN`,
  `NOT_NULL_NO_DEFAULT`, `ALTER_COLUMN_TYPE`, `INDEX_NO_CONCURRENTLY`,
  `FK_NO_NOT_VALID`, `UNIQUE_NO_CONCURRENTLY`, `RENAME_OBJECT`,
  `UPDATE_NO_WHERE`, `DELETE_NO_WHERE`) — see
  [docs/detector-reference.md](docs/detector-reference.md).
- Dependency CVE scan against `api.osv.dev` for `requirements.txt`,
  `pyproject.toml`, `poetry.lock`, `package.json`, `package-lock.json`,
  `go.mod`, `cargo.lock`.
- Secret scanning on added lines with masked output (`scan_secrets`):
  AWS/GitHub/Slack/Stripe/Google keys, private-key blocks, JWTs,
  `password|secret|api_key|token` literals, and a Shannon-entropy heuristic
  (`detectors/secrets.py`).
- Multi-language pattern checks for JS/TS, Go, Java, Ruby, PHP — eval/shell
  sinks, DOM-XSS sinks, unsafe deserialization, weak crypto, leftover
  debuggers (`scan_source_patterns`, `detectors/langs.py`).
- Dockerfile and GitHub Actions lints: unpinned bases/actions,
  pipe-to-shell installs, baked-in secrets, missing `USER`,
  `pull_request_target` head-checkout, `run:` script injection
  (`lint_config_file`, `detectors/confchecks.py`).
- `read_file` tool: sandboxed repo reads (path-escape refusal, 40 KB cap).

**Surfaces**

- CLI (`gitport`): `check` (`--base/--head/--staged/--diff-file`, `--strict`,
  `--fail-open`, `--json`, `--quiet`), `index`, `install-hook`, `serve`,
  `mcp`, `version`. Rich table output or machine-readable `CheckReport` JSON.
- Output formats via `--format`: `rich`, `json`, `markdown` (PR comments),
  `sarif` 2.1.0 (code scanning), `junit` (test-report ingestion)
  (`formatters.py`).
- Policy-as-code: `.gitport/policy.toml` auto-loads per check (or
  `--policy PATH`); `severity_floor`, `fail_on`, `[[suppress]]` with audit
  reasons, `[[escalate]]` hot zones (`policy.py`,
  `examples/policy.toml`).
- GitHub integration: `--post-comment --pr --repo-slug` upserts a marked
  PR comment; `--set-status --sha --repo-slug` posts a commit status
  (`github.py`, `GITHUB_TOKEN`).
- Git hooks: `install-hook pre-push` (client-side) and `pre-receive`
  (server-side, bare repo); empty-tree base handling for new refs.
- MCP server (`gitport-mcp` / `gitport mcp`, stdio): `gitport_check`,
  `gitport_check_diff`, `gitport_index_rules`, plus keyless
  `gitport_lint_migration`, `gitport_analyze_python`, `gitport_scan_secrets`.
  Compatible with `mcp` 1.x and 2.x (`FastMCP`/`MCPServer` shim).
- REST API (`gitport serve`, FastAPI): `GET /healthz`, `POST /v1/check`,
  `POST /v1/index`, `GET /v1/schema`, `GET /v1/checks[/{id}]`,
  `GET /v1/stats`; optional `Authorization: Bearer` auth via
  `GITPORT_API_TOKEN`; shared `{"error": {type, message, request_id}}`
  envelope. ASGI middleware (`server.py`): request IDs, body cap
  (`GITPORT_MAX_REQUEST_BYTES` → 413), per-IP sliding-window rate limit
  (`GITPORT_RATE_LIMIT_RPM` → 429 + `Retry-After`, `/healthz` exempt), CORS
  allowlist (`GITPORT_CORS_ORIGINS`), optional JSON logs
  (`GITPORT_LOG_FORMAT=json`). SQLite audit store for check reports
  (`reports.py`, `GITPORT_REPORTS_DB`, `GITPORT_STORE_REPORTS=0` disables).

**Configuration**

- All settings via env / `.env` (`GITPORT_*` prefix, `COHERE_API_KEY`):
  model selection, index/rules paths, retrieval K, diff size cap, agent
  budget, API token, fail-open, policy path, reports DB, API hardening knobs
  (`config.py`, `.env.example`).

**Project**

- 119-test pytest suite with `FakeCohereClient` — fully offline
  (`tests/conftest.py`).
- Sample rules corpus (`docs/rules/`); examples for GitHub Actions, GitLab
  CI, MCP client config and policy files (`examples/`); CI workflow on
  Python 3.11–3.13; `py.typed` marker; Dockerfile + docker-compose.
- MIT license.

[Unreleased]: https://github.com/rayhankhilji/gitport-ai/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/rayhankhilji/gitport-ai/releases/tag/v0.1.0
