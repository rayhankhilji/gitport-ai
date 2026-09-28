# Security Rules for Application Code

## Forbidden constructs

- `eval`, `exec`, `compile`, `__import__` on anything derived from input.
- `subprocess` with `shell=True` unless the command is a constant string and
  the call site is reviewed by security.
- `pickle.load`/`pickle.loads` on data that crosses a trust boundary.
- `yaml.load` without `SafeLoader`.
- `hashlib.md5` / `hashlib.sha1` for anything security-relevant (tokens,
  signatures, passwords). Fine for cache keys only.

## Secrets

- No credentials, tokens, or private keys in source. Use the secret store.
- Never log request bodies at INFO or above — they contain PII.

## HTTP

- `requests` calls must set an explicit `timeout`.
- `verify=False` is never acceptable outside a test fixture.
