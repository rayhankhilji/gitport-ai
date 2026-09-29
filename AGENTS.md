# gitport — agent notes

## Build / test / lint

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q            # full suite, no API key needed (FakeCohereClient)
.venv/bin/ruff check src tests
```

## Layout

- `src/gitport/` — package. Pipeline: `diff.py` → `rules.py`+`store.py` →
  `agent.py` (tool loop) → `gate.py` (structured verdict); orchestrated by
  `engine.py`. Surfaces: `cli.py`, `mcp_server.py`, `api.py`.
- `src/gitport/detectors/` — deterministic scanners wrapped as agent tools.
- `docs/rules/` — sample internal-rules corpus; `gitport index` embeds it.
- `tests/` — pytest; `conftest.py` has `FakeCohereClient` + sample diff.

## Conventions

- Cohere client is always injectable — never call `cohere` inside pure
  modules; tool executors return JSON-able dicts, never raise.
- Errors are data inside tools; engine failures go through `error_verdict`
  (fail-closed by default, `GITPORT_FAIL_OPEN=1` downgrades to WARNING).
- ruff: line-length 110, `zip(..., strict=True)`, `raise ... from` in excepts.
- Commit style: short lowercase imperative, no AI attribution.
