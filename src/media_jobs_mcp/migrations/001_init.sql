-- Jobs, their per-file technical specs, pipeline errors and the runbook corpus used for RAG.
-- Every write path is an upsert on a natural key, so retries and replays never duplicate rows.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS jobs (
    job_code      text PRIMARY KEY,
    source_id     text NOT NULL UNIQUE,          -- id in the upstream system; idempotency key for intake
    title         text NOT NULL,
    status        text NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'processing', 'completed', 'failed', 'ignored')),
    retry_round   integer NOT NULL DEFAULT 0,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE SEQUENCE IF NOT EXISTS job_code_seq;

CREATE TABLE IF NOT EXISTS job_errors (
    id            bigserial PRIMARY KEY,
    job_code      text NOT NULL REFERENCES jobs (job_code),
    task_name     text NOT NULL,
    category      text NOT NULL CHECK (category IN ('transient', 'input', 'technical', 'unknown')),
    http_status   integer,
    message       text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS job_errors_job_code_idx ON job_errors (job_code, created_at DESC);

-- Request-level idempotency (Idempotency-Key header): same key returns the stored response.
-- A key is bound to one job; `response` is NULL while the first request is still in flight.
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key           text PRIMARY KEY,
    job_code      text NOT NULL,
    response      jsonb,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS runbook_chunks (
    doc           text NOT NULL,
    chunk_no      integer NOT NULL,
    heading       text NOT NULL,
    content       text NOT NULL,
    tsv           tsvector GENERATED ALWAYS AS (to_tsvector('english', heading || ' ' || content)) STORED,
    embedding     vector(256) NOT NULL,
    PRIMARY KEY (doc, chunk_no)
);
CREATE INDEX IF NOT EXISTS runbook_chunks_tsv_idx ON runbook_chunks USING gin (tsv);
