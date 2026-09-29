# Detector reference

Every deterministic check gitport ships. These run inside the agent loop as
tools (`tools.py`) — the model decides which to invoke against the diff, the
detectors return facts, and the gate weighs them. Nothing here is a regex
vendored from the model's imagination; every finding is a named rule or a
parse result.

Tools: `analyze_python_file`, `lint_migration_file`, `scan_manifest`,
`scan_secrets`, `scan_source_patterns`, `lint_config_file`, `read_file`.

## SQL migration hazards — `lint_migration_file`

`analysis.lint_sql_migration`, per-line rules in `_SQL_RULES`. Runs on `.sql`
files and paths matching `migration|alembic|migrate|ddl`. Skips `--` comments
and blank lines. `hazardous` is true when any finding is high or critical.

| ID | What it catches | Severity | Remediation |
|---|---|---|---|
| `DROP_TABLE` | `DROP TABLE` — destroys data, breaks anything still reading it | critical | rename to `*_deprecated`, drop after a full deploy cycle |
| `DROP_COLUMN` | `DROP COLUMN` — old code versions still reading it crash during rollout | critical | deploy code that ignores the column first; drop in a later migration |
| `NOT_NULL_NO_DEFAULT` | `ADD COLUMN … NOT NULL` without `DEFAULT` — rewrites/locks the whole table on Postgres < 11 | high | add nullable or with `DEFAULT`, backfill, then `SET NOT NULL` |
| `ALTER_COLUMN_TYPE` | `ALTER COLUMN … TYPE` / `SET DATA TYPE` — table rewrite under `ACCESS EXCLUSIVE` | high | new column, dual-write, backfill, swap |
| `INDEX_NO_CONCURRENTLY` | `CREATE [UNIQUE] INDEX` without `CONCURRENTLY` — blocks writes | high | `CREATE INDEX CONCURRENTLY`, outside a transaction |
| `FK_NO_NOT_VALID` | `ADD … FOREIGN KEY` without `NOT VALID` — full-table scan under lock | medium | add `NOT VALID`, then `VALIDATE CONSTRAINT` separately |
| `UNIQUE_NO_CONCURRENTLY` | `ADD CONSTRAINT … UNIQUE` without `USING INDEX` — exclusive lock while scanning | medium | `CREATE UNIQUE INDEX CONCURRENTLY` then `ADD CONSTRAINT … USING INDEX` |
| `RENAME_OBJECT` | `RENAME TABLE\|COLUMN\|TO` — breaks old code versions mid-deploy | medium | add the new name alongside, migrate reads/writes, remove the old name later |
| `UPDATE_NO_WHERE` | `UPDATE t SET` without `WHERE` — locks every row | medium | batch with `WHERE` and key ranges |
| `DELETE_NO_WHERE` | `DELETE FROM t` without `WHERE` — removes every row | high | add `WHERE`; if intentional, `TRUNCATE` and confirm backups |

## Python AST — `analyze_python_file`

`analysis.analyze_python_source` parses real syntax (with dedent/fragment
fallbacks for diff-only snippets) and reports functions, classes, imports —
plus dangerous call sites from `_DANGEROUS_CALLS`. These findings carry no
hardcoded severity; the gate weighs them.

| Call | Why it matters |
|---|---|
| `eval`, `exec` | executes arbitrary code from a string |
| `compile` | dynamic code compilation; usually unnecessary |
| `__import__` | dynamic import that bypasses static analysis |
| `os.system`, `os.popen` | shell command execution — prefer `subprocess` without `shell=True` |
| `subprocess.*(shell=True)` | `shell=True` enables command injection |
| `pickle.loads`, `pickle.load` | deserializing untrusted pickle data executes code |
| `marshal.loads` | unsafe deserialization |
| `yaml.load` | without a `SafeLoader`, executes arbitrary objects |
| `hashlib.md5`, `hashlib.sha1` | broken hash algorithms; use sha256+ |
| `tempfile.mktemp` | insecure temp file (race condition); use `mkstemp` |

## Secrets — `scan_secrets`

`detectors.secrets.scan_text_for_secrets` runs on the diff's **added lines**.
All findings are severity `high`. Values are never returned — only a masked
`match_preview` (first 6 chars + `…`), so transcripts stay safe.

| Kind | Pattern | Remediation |
|---|---|---|
| `aws-access-key` | `AKIA[0-9A-Z]{16}` | revoke in IAM; move to a secrets manager or env var |
| `github-token` | `ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_` | revoke in GitHub settings; use a secret store or `GITHUB_TOKEN` |
| `slack-token` | `xox[baprs]-…` | revoke in the Slack app console; load from env |
| `stripe-live-key` | `sk_live_`, `rk_live_` | roll in the Stripe dashboard — live keys charge real cards |
| `google-api-key` | `AIza[0-9A-Za-z_-]{35}` | restrict/regenerate in Google Cloud console |
| `private-key` | `-----BEGIN … PRIVATE KEY-----` | remove the key material; rotate — assume leaked |
| `jwt` | `eyJ…` three-part token | treat as compromised; never commit live tokens |
| `generic-secret` | `password\|passwd\|secret\|api_key\|token` assigned a quoted literal ≥ 8 chars | read from the environment or a secrets manager |
| `high-entropy-string` | ≥ 20 chars, Shannon entropy > 4.5, ≥ 3 character classes | confirm and rotate or remove |

Entropy suppressions: pure-hex/numeric tokens, and lines containing URLs or
`sha1|sha256|sha384|sha512|md5|integrity|checksum|digest|fingerprint`, are
skipped — those tokens are expected hashes, not secrets.

## Multi-language patterns — `scan_source_patterns`

`detectors.langs.scan_source_patterns` — per-line rules on added lines for
non-Python source (Python files get the AST analyzer; a couple of line-level
Python rules live here too). Extensions: `.js/.jsx/.mjs/.cjs/.ts/.tsx/.mts/.cts`,
`.go`, `.java`, `.rb`, `.php`, `.py`.

### JavaScript / TypeScript

| ID | Catches | Severity |
|---|---|---|
| `eval` | `eval(` arbitrary code | high |
| `new-function` | `new Function(` — eval with extra steps | high |
| `shell-exec` | `exec/execSync/execFile/spawn/fork(` — only flagged when `child_process` is imported | high |
| `document-write` | `document.write(ln)(` DOM-XSS sink | medium |
| `innerhtml` | `innerHTML =` unsanitized assignment | medium |
| `dangerously-set-inner-html` | React `dangerouslySetInnerHTML` | medium |
| `child-process` | `child_process` import/usage | medium |
| `localstorage-secret` | token/secret/password/api_key into `localStorage` | medium |
| `debugger` | leftover `debugger;` | medium |
| `console-log` | leftover `console.log(` | low |

### Go

| ID | Catches | Severity |
|---|---|---|
| `exec-sprintf` | `exec.Command(fmt.Sprintf(…))` — command injection | high |
| `unsafe` | `unsafe.` — bypasses memory safety | medium |
| `http-no-timeouts` | `http.ListenAndServe(` — no timeouts (Slowloris) | medium |

### Java

| ID | Catches | Severity |
|---|---|---|
| `runtime-exec` | `Runtime.getRuntime().exec(` | high |
| `objectinputstream` | `new ObjectInputStream(` — unsafe deserialization | high |
| `weak-cipher` | `Cipher.getInstance("…DES…/…ECB…")` | high |

### Ruby

| ID | Catches | Severity |
|---|---|---|
| `eval` | `eval(` | high |
| `system` | `system(` | high |
| `backticks` | `` `cmd` `` shell execution | high |
| `binding-pry` | leftover `binding.pry` | medium |

### PHP

| ID | Catches | Severity |
|---|---|---|
| `eval` | `eval(` | high |
| `shell-exec` | `shell_exec(` | high |
| `unserialize` | `unserialize(` — object injection | high |

### Python (line-level complement to the AST analyzer)

| ID | Catches | Severity |
|---|---|---|
| `pdb` | `pdb.set_trace(` | medium |
| `breakpoint` | `breakpoint(` | medium |

## CI / Dockerfile — `lint_config_file`

`detectors.confchecks.lint_config` — `Dockerfile*` / `*.dockerfile` and
`.github/workflows/*.{yml,yaml}` paths, on added lines.

### Dockerfile

| Rule | Catches | Severity | Remediation |
|---|---|---|---|
| `remote-add` | `ADD https?://` — unverified remote content at build time | high | `COPY` local files, or curl + checksum |
| `pipe-to-shell` | `curl\|wget … \| (sudo) (ba)sh` — runs remote scripts sight-unseen | high | download, verify checksum/signature, then run |
| `env-secret` | `ENV`/`ARG` `*_KEY\|SECRET\|PASSWORD\|TOKEN` literal — lands in layers/logs | high | `--mount=type=secret`, never a literal |
| `unpinned-base-image` | `FROM image` with no tag or `:latest` | medium | pin a tag or sha256 digest |
| `root-user` | `USER root`/`USER 0` | medium | create and switch to a non-root user |
| `missing-user` | no `USER` directive at all | medium | add a non-root `USER` before entrypoint |

### GitHub Actions

| Rule | Catches | Severity | Remediation |
|---|---|---|---|
| `unpinned-action` | `uses: actions/foo@v1` — mutable ref | medium | pin the commit SHA (`@<40-hex>`) |
| `pull-request-target-checkout` | `pull_request_target` + checkout of `github.event.pull_request.head`/`head_ref` | high | use `pull_request`, or never check out the PR head |
| `script-injection` | `${{ github.event.* }}` interpolated inside a `run:` script | high | pass via `env:` and quote `"$VAR"` |

## Dependency CVEs — `scan_manifest`

`analysis.scan_dependencies` batch-queries `api.osv.dev/v1/querybatch` for
pinned deps parsed from the manifest. Severity comes back as the OSV record's
score string (e.g. a CVSS vector) or `unknown`. A network failure returns
`{"ok": false, …}` — it never blocks the gate on its own.

| Manifest | Ecosystem |
|---|---|
| `requirements.txt`, `pyproject.toml`, `poetry.lock` | PyPI |
| `package.json`, `package-lock.json` | npm |
| `go.mod` | Go |
| `cargo.lock` | crates.io |

## Context reads — `read_file`

Not a check: a sandboxed read of a changed file's post-change content (repo
working tree, else diff-added lines). Paths escaping the repo root are
refused; output truncates at 40 KB.

## How findings become verdicts

Detector findings are evidence, not verdicts. The gate (`gate.py`) cites them
in `critical_flaws` with `severity ∈ {low, medium, high, critical}`; policy
(`policy.py`) can then drop findings below `severity_floor`, suppress
accepted risks, or escalate flaws on hot paths — see
[deployment.md](deployment.md#e-policy-file).
