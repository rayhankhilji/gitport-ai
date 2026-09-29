# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| `0.1.x` | ✅ current release line |

Older commits have no support window — upgrade to the latest `0.1.x`.

## Reporting a vulnerability

**Do not open a public issue.** Report privately through GitHub Security
Advisories:

<https://github.com/rayhankhilji/gitport-ai/security/advisories/new>

Include: affected version/commit, the diff or input that triggers it, and the
impact (gate bypass, secret disclosure, sandbox escape). You should get a
first response within a few days; fixes land on `main` and ship in the next
patch release.

## Security posture

gitport sits on the merge path, so its failure modes are designed to be safe:

- **Fail closed by default.** If Cohere is unreachable, the key is missing, or
  the verdict can't be parsed, the verdict is `FAILED` and `gitport check`
  exits `1`. `GITPORT_FAIL_OPEN=1` / `--fail-open` downgrades engine errors to
  `WARNING` — opt in deliberately, never on a gate you rely on.
- **Secrets are masked in detector output.** `scan_secrets` reports the
  pattern and line that matched, not the credential — the model (and logs,
  and the audit transcript) never see the secret value.
- **Sandboxed file reads.** Agent tools resolve paths through
  `ToolContext.source_for`: anything escaping the repo root is refused, and
  reads are capped at 40 KB. Tools are read-only — the agent cannot write
  files or run shell commands.
- **Bearer auth on the API.** Set `GITPORT_API_TOKEN` and every `/v1/*` route
  requires `Authorization: Bearer <token>`. `/healthz` stays open for
  liveness probes. With no token the service assumes localhost/CI use only —
  do not expose it to a network.
- **API hardening middleware.** Per-client-IP sliding-window rate limiting
  (`GITPORT_RATE_LIMIT_RPM`, default 120 → 429 + `Retry-After`, `/healthz`
  exempt), a `Content-Length` body cap (`GITPORT_MAX_REQUEST_BYTES`, default
  5 MB → 413), CORS off unless `GITPORT_CORS_ORIGINS` is set, and an
  `X-Request-ID` on every response for log correlation. Rate limiting is
  in-memory per worker — enforce at the proxy for multi-replica deploys.
- **Bounded inputs.** Diff digests are truncated at `GITPORT_MAX_DIFF_CHARS`
  (default 60 k) and the agent loop is capped at `GITPORT_AGENT_MAX_STEPS`
  (default 8), so a hostile or enormous diff can't burn unbounded tokens.
- **Non-root container.** The shipped `Dockerfile` runs as an unprivileged
  `gitport` user.

## Data leaving your machine

A check sends the diff digest, retrieved rule excerpts, and tool results to
Cohere (`embed`, `rerank`, `chat`) and pinned manifest contents to
`api.osv.dev`. If your diffs may contain secrets, run `scan_secrets` policy
seriously — and prefer reviewing what the tools return before assuming a diff
is clean. `COHERE_API_KEY` is read from the environment or `.env` and is
never logged or included in verdict output.
