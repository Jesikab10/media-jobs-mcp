# Security

## Reporting

Please report vulnerabilities privately through GitHub's
[security advisories](../../security/advisories/new), not in public issues.

## Model

| Boundary | Control |
|---|---|
| Client → MCP server (HTTP) | OAuth 2.1 bearer tokens validated against the IdP's JWKS: signature (RS256/ES256 only), `iss`, `aud`, `exp`, `nbf`. Scopes per tool (`jobs:read`, `jobs:write`). |
| Client → MCP server (stdio) | Local process acting for the user who launched it; no network exposure. |
| MCP server → jobs API | Separate service credential: required, 24+ characters, the `.env.example` placeholder is rejected at startup. The end user's token is never forwarded. The database URL is only configured on the API and has no default. |
| Upstream text → model | Error messages, task names, job titles and runbook passages are redacted (emails, phones, tokens, signed URLs). In the agent prompt, every piece of upstream text and the operator's question are wrapped in `<untrusted>` tags with `<` and `>` escaped, so the text cannot close the boundary. |
| Model → actions | In the bundled agent, writes only happen after its human-approval node. The `retry_job` MCP tool itself does not ask for approval; any other MCP client is responsible for confirming (Claude Desktop asks before running a tool). |
| Tool arguments | Job codes are validated against a strict pattern by the MCP tool schema and again by the API route; path segments are URL-encoded. |
| Idempotency keys | Bound to one job: reusing a key for a different job is rejected (422). |
| Model → user | Final report re-scanned for leaks before it is returned. |
| Files | `probe_media` resolves paths inside a fixed root; `ffprobe` runs with an argument list, no shell. |

## Development guardrails

`.claude/hooks/guard.py` blocks pushes to `main`, force pushes, deploy commands and access to
secret files from Claude Code. It is defence in depth, not a sandbox: branch protection, CI-held
deploy credentials and keeping secrets out of the working tree are the actual controls.

## Not in scope of this demo

- The development token issuer (`devtokens.py`) is for local use only.
- Failure injection (`MJM_FAILURE_INJECTION`) must stay off outside local environments.
