# Contributing

## Dev setup

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"

.venv/bin/pytest                # full suite — mocked Cohere client, no API key
.venv/bin/ruff check src tests  # lint
```

The suite never touches the network: `tests/conftest.py` ships
`FakeCohereClient`, a scripted stand-in for `cohere.ClientV2` (chat / embed /
rerank). New pipeline behavior should be testable through it — pass it as the
`client` argument to `run_check` / `build_index` / `retrieve_rules`.

## Layout

`src/` layout; the package is `gitport`. Three surfaces — `cli.py`,
`mcp_server.py`, `api.py` — all delegate to one pipeline in `engine.py`:

```
diff.py → rules.py + store.py → agent.py → gate.py
            (embed → sqlite → rerank)   (tool loop)   (structured verdict)
```

Deterministic scanners live under `analysis.py` (Python AST, SQL migration
lint, OSV dependency scan) and `detectors/` (secrets, multi-language
patterns, CI/Dockerfile lints). Everything the model can invoke is wired
through `tools.py`. Supporting cast: `formatters.py` (json / markdown /
sarif / junit output), `policy.py` (`.gitport/policy.toml`),
`github.py` (PR comments, commit statuses), `reports.py` (SQLite audit log),
`server.py` (ASGI middleware — request IDs, body cap, rate limit).

## Conventions

- **Injectable Cohere client.** Never `import cohere` in a pure module — the
  client is built once in `engine.make_client` and passed down. This is what
  keeps the test suite offline.
- **Errors are data inside tools.** Tool executors return JSON-able dicts and
  never raise; a failed check returns `{"ok": False, "error": ...}` the model
  can read. Engine-level failures go through `error_verdict` — fail closed by
  default, `GITPORT_FAIL_OPEN=1` downgrades to a warning.
- **Pydantic for everything that crosses a boundary.** `models.py` owns
  `FileDiff`, `Verdict`, `CheckReport`, `ToolCallRecord`. The verdict model
  mirrors `gate.VERDICT_SCHEMA` — change both together.
- **Tool registry pattern.** Adding an agent tool means three edits in
  `tools.py`: a `_fn(...)` entry in `TOOL_SCHEMAS`, a branch in
  `execute_tool`, and a `_t_*` executor. Resolve content through
  `ToolContext.source_for` (repo file, fall back to diff-added lines) or
  `added_source_for` (diff-added lines only) — never touch the filesystem
  directly. `source_for` refuses paths that escape `repo_root` and caps reads
  at 40 KB.
- **Deterministic checks stay deterministic.** Scanners report facts (rule
  IDs, line numbers, matched patterns); severity judgment belongs to the
  gate, not the regex.
- Style: ruff `line-length = 110`, `zip(..., strict=True)`,
  `raise ... from e` in except blocks, `from __future__ import annotations`.
  Python ≥ 3.10.
- Commit style: short lowercase imperative, no AI attribution.

## Testing

```bash
.venv/bin/pytest -q                 # everything
.venv/bin/pytest tests/test_detectors.py tests/test_policy.py
```

`FakeCohereClient` scripts the agent loop (`tool_plan`) and the verdict
(`verdicts`), including the malformed-JSON repair retry — use it instead of
mocking internals.

## PR checklist

- [ ] `.venv/bin/pytest` green; `.venv/bin/ruff check src tests` clean
- [ ] New tools registered in `TOOL_SCHEMAS` **and** `execute_tool`, with a
      test through `FakeCohereClient`
- [ ] Schema changes update `gate.VERDICT_SCHEMA` + `models.Verdict` +
      `GET /v1/schema` consumers
- [ ] New config knobs go through `config.Settings` (env-prefixed
      `GITPORT_*`), not `os.environ` reads
- [ ] Detectors return `{"ok", "file", "findings"/...}` dicts — no
      exceptions, no raw secrets in output (mask matches)
- [ ] New output formats go through `formatters.render`; policy logic stays
      in `policy.py` and never mutates the original verdict
- [ ] Docs updated: `README.md` for user-facing behavior,
      `docs/detector-reference.md` for new check IDs,
      `CHANGELOG.md` under Unreleased
- [ ] Commit message: short lowercase imperative
