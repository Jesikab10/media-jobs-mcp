# How I work with AI coding tools

I use Claude Code as the main implementation tool. The speed is real; so is the failure mode:
plausible code that was never checked against the real library, the real database or the real
data. The workflow below exists to catch that.

## Rules

1. **Read the source before writing against it.** Library APIs, schemas and payloads are checked
   in the installed code or the live system, not recalled.
2. **Evidence and inference are labelled separately.** "Verified in `file:line`" and "inference"
   never share a sentence.
3. **One branch per change, PR into `main`.** A hook blocks pushes to `main`.
4. **Tests and evals are the definition of done.** Not "it ran once".
5. **Deploys and secrets stay with a human.** The hook blocks deploy commands and writes to
   secret files.

The guardrails are in [`.claude/`](../.claude/): a permission allowlist, a `PreToolUse` hook and
two review agents (`security-reviewer`, `verifier`).

## What went wrong while building this project, and what caught it

These are real, from the build of this repository.

| What the first draft did | What caught it | Fix |
|---|---|---|
| Wrote the server against `FastMCP`, the API of MCP Python SDK 1.x | Reading the installed package before going further: `mcp` 2.3 renamed it to `MCPServer` and changed the client and auth APIs | Rewrote against the installed SDK, pinned `mcp>=2.3,<3` |
| Set `conn.row_factory = dict_row` on pooled connections | A test failed with `KeyError: 0` in a *different* endpoint: the setting stayed on the connection after it went back to the pool | Row factories set per cursor, never on a pooled connection |
| `from __future__ import annotations` in the FastAPI module | Every request returned `422 field required: pool`: FastAPI could not resolve a locally defined `Annotated` alias | Removed the import from that module |
| The eval harness reset the database between cases by dropping all tables | The citation evals failed: the reset also wiped the runbook index, so retrieval returned nothing | Reset truncates job data only; the corpus is kept |
| Gave the service token a default value | Self-review against the security rules: a deployment could start with a well-known secret | Required field with a minimum length; startup fails closed |
| Step-budget test expected an exception to propagate | The exception surfaced wrapped in the MCP client's task group | Changed the behaviour, not the test: a budget hit now ends the run with an explicit "stopped without acting" report |

None of these were visible by reading the code. They showed up by running it against the real
thing: the installed SDK, a real PostgreSQL, and the eval suite.

Before publishing, an independent review agent (the `security-reviewer` definition in
`.claude/agents/`) audited the repository and found eight real issues. All are fixed, and each
has a regression test:

| Finding | Fix |
|---|---|
| The guard hook missed `git -C . push main`, `push -f`, `cat .env` and upper-case file names | Matches git global options, `-f` and `+ref`, shell access to secrets, case-insensitive paths, all edit tools; documented as defence in depth |
| An idempotency key used for job A, replayed for job B, returned A's response and silently skipped B | Keys are bound to a job and reserved before the write; reuse for another job is rejected (422) |
| A `</untrusted>` inside an error message closed the prompt boundary early | Upstream text is escaped inside the tags; runbook passages are wrapped too |
| `job_code` went into the URL path unvalidated (`../runbooks/search`) | Strict pattern in the tool schema and the API route, plus URL encoding |
| A non-JSON error page from a proxy escaped error handling | Parsed defensively; always surfaces as a clean API error |
| The database URL had a default with a password; the `.env.example` placeholder passed as a real token | No default; minimum length; placeholder rejected at startup |
| Rate-limit buckets grew without bound per client id | Bounded registry with LRU eviction |
| Gitleaks job lacked `pull-requests: read` | Added to the job's permissions |

Two passes, two different kinds of bugs: running the code finds what breaks; an adversarial
review finds what works but should not.
