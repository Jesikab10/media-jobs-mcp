# Design decisions

Short records: context, decision, what it costs. Newest last.

## 1. The graph decides when tools run; the LLM makes the judgement call

**Context.** A free-form tool-calling agent can call any tool in any order. For an agent that can
change production state, that makes every run a new, untested path.

**Decision.** The workflow is an explicit LangGraph state machine. Reads happen in
`investigate`; the only write happens in `act`, reachable only through `approve`. The model sits
behind a `Reasoner` interface with two methods (`classify`, `decide`) and returns structured
output.

**Cost.** Less flexible than an open agent loop: a new kind of action means a new node and edge.
In exchange, the same graph runs in CI with a deterministic reasoner, every write path has a
test, and an injected instruction cannot reach a write tool by itself.

## 2. Selective retries, and throttling before retrying

**Context.** Downstream services fail in different ways. A 429 means "busy, come back later"; a
503 is often an instance being recycled; a 422 means the request is wrong; a read timeout means
the server may still be working.

**Decision.** Retry 429 (honouring `Retry-After`), 502/503/504 and connection errors, with capped
exponential backoff and jitter. Never retry other 4xx. Never retry read timeouts: re-sending a
heavy write after a timeout can duplicate work. Throttle outgoing calls with a token bucket so
the client avoids most 429s instead of only reacting to them.

**Cost.** A timed-out write needs a manual replay; the idempotency key makes that replay safe.

## 3. Idempotency everywhere a write can be repeated

**Context.** Agents loop, networks drop responses, people click twice.

**Decision.** Job intake is keyed by the upstream `source_id`
(`INSERT … ON CONFLICT DO NOTHING`, then read back; the response says whether it was a replay).
`retry_job` requires an `Idempotency-Key`; the first response is stored and returned for any
repeat. The API client reuses the same key across its own internal retries.

**Cost.** Every write endpoint needs a key and a place to store it.

## 4. The MCP server is an OAuth resource server, with its own downstream credential

**Context.** The MCP authorization spec separates the authorization server (issues tokens) from
the resource server (accepts them), and forbids passing a client's token through to other
services.

**Decision.** Validate JWTs against the IdP's JWKS (`iss`, `aud`, `exp`, `nbf`, algorithm
allow-list) and map scopes to tools. Publish Protected Resource Metadata (RFC 9728) so clients can
discover the issuer. Call the jobs API with a separate service credential that is required at
startup (no default value).

**Cost.** An IdP is required in any shared environment. Development uses a local signing key and
JWKS file, clearly marked as development only.

## 5. Treat upstream text as untrusted, at two points

**Context.** Error messages come from systems the agent does not control. They can contain
customer data or text written to look like instructions.

**Decision.** Redact emails, phone numbers, tokens and signed URLs in `list_job_errors` before
anything reaches a model. Label error text as untrusted in the tool output and in the prompt.
Scan the final report again before returning it, because a model can reintroduce what the input
filter removed.

**Cost.** Regex redaction has false positives (some numeric strings look like phone numbers).
That is the right side to err on for this data.

## 6. Retrieval: hybrid search in PostgreSQL, deterministic embedder by default

**Context.** The runbook corpus is small and keyword-heavy (HTTP codes, codec names). Pure vector
search misses exact terms; pure full-text search misses paraphrases.

**Decision.** Full-text search and pgvector cosine search, fused with reciprocal rank fusion in a
single SQL query. The default embedder is feature hashing: no download, no key, identical results
in CI and offline. Citations are checked against the passages actually retrieved; anything else
is dropped.

**Cost.** Hashing is a lexical baseline, not semantic understanding. A real embedding model is a
drop-in `Embedder` implementation with the same dimension.
