"""Jobs API: the internal system the MCP server integrates with.

It behaves like a real dependency on purpose: service authentication, per-client rate limits
(429 + Retry-After), idempotent writes, and optional failure injection for demos and tests.
"""

import hmac
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path as FsPath
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Request
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from media_jobs_mcp import rag
from media_jobs_mcp.config import Settings, get_settings
from media_jobs_mcp.db import create_pool, migrate
from media_jobs_mcp.models import JOB_CODE_PATTERN, Job, JobCreate, JobError, JobSubmitResult
from media_jobs_mcp.ratelimit import BucketRegistry, retry_after_header
from media_jobs_mcp.seed import seed

JOB_COLUMNS = "job_code, source_id, title, status, retry_round, created_at, updated_at"


def create_app(
    settings: Settings | None = None,
    pool: ConnectionPool | None = None,
    clock: Callable[[], float] | None = None,
    runbooks_dir: FsPath | None = FsPath("runbooks"),
) -> FastAPI:
    settings = settings or get_settings()
    buckets = (
        BucketRegistry(settings.api_rate_per_s, settings.api_burst, clock)
        if clock
        else BucketRegistry(settings.api_rate_per_s, settings.api_burst)
    )
    embedder = rag.HashingEmbedder()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = pool is None
        if pool is None and settings.database_url is None:
            raise RuntimeError("MJM_DATABASE_URL is required to run the jobs API")
        app.state.pool = pool or create_pool(
            settings.database_url.get_secret_value()  # type: ignore[union-attr]
        )
        migrate(app.state.pool)
        if settings.seed_demo_data:
            seed(app.state.pool)
        if runbooks_dir and runbooks_dir.is_dir():
            rag.ingest_runbooks(app.state.pool, runbooks_dir, embedder)
        yield
        if owned:
            app.state.pool.close()

    app = FastAPI(title="Jobs API", version="0.1.0", lifespan=lifespan)

    def db(request: Request) -> ConnectionPool:
        pool_: ConnectionPool = request.app.state.pool
        return pool_

    Pool = Annotated[ConnectionPool, Depends(db)]
    JobCode = Annotated[str, Path(pattern=JOB_CODE_PATTERN)]

    @app.middleware("http")
    async def guard(request: Request, call_next: Any) -> Any:
        if request.url.path == "/healthz":
            return await call_next(request)
        # Service authentication: constant-time comparison, no detail in the error.
        auth = request.headers.get("authorization", "")
        expected = f"Bearer {settings.service_token.get_secret_value()}"
        if not hmac.compare_digest(auth.encode(), expected.encode()):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)

        client_id = request.headers.get("x-client-id", "anonymous")[:64]
        wait = buckets.get(client_id).try_acquire()
        if wait > 0:
            return JSONResponse(
                {"detail": "rate limit exceeded"},
                status_code=429,
                headers={"Retry-After": retry_after_header(wait)},
            )

        simulate = request.headers.get("x-simulate-status")
        if settings.failure_injection and simulate in {"429", "502", "503"}:
            headers = {"Retry-After": "1"} if simulate == "429" else {}
            return JSONResponse({"detail": "simulated"}, status_code=int(simulate), headers=headers)
        return await call_next(request)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/jobs", response_model=JobSubmitResult)
    def submit_job(body: JobCreate, pool: Pool) -> JobSubmitResult:
        # Idempotent intake: the upstream source_id is the natural key. A replay returns the
        # existing job instead of creating a duplicate (ON CONFLICT DO NOTHING + read back).
        with pool.connection() as conn, conn.transaction():
            row = (
                conn.cursor(row_factory=dict_row)
                .execute(
                    f"""
                INSERT INTO jobs (job_code, source_id, title)
                VALUES ('MJ-' || lpad(nextval('job_code_seq')::text, 4, '0'), %s, %s)
                ON CONFLICT (source_id) DO NOTHING
                RETURNING {JOB_COLUMNS}
                """,  # noqa: S608 - column list is a module constant
                    (body.source_id, body.title),
                )
                .fetchone()
            )
            created = row is not None
            if row is None:
                row = (
                    conn.cursor(row_factory=dict_row)
                    .execute(
                        f"SELECT {JOB_COLUMNS} FROM jobs WHERE source_id = %s",  # noqa: S608
                        (body.source_id,),
                    )
                    .fetchone()
                )
        return JobSubmitResult(job=Job.model_validate(row), created=created)

    def _get_job(pool: ConnectionPool, job_code: str) -> Job:
        with pool.connection() as conn:
            row = (
                conn.cursor(row_factory=dict_row)
                .execute(
                    f"SELECT {JOB_COLUMNS} FROM jobs WHERE job_code = %s",  # noqa: S608
                    (job_code,),
                )
                .fetchone()
            )
        if row is None:
            raise HTTPException(404, "job not found")
        return Job.model_validate(row)

    @app.get("/jobs/{job_code}", response_model=Job)
    def get_job(job_code: JobCode, pool: Pool) -> Job:
        return _get_job(pool, job_code)

    @app.get("/jobs/{job_code}/errors", response_model=list[JobError])
    def list_errors(job_code: JobCode, pool: Pool, limit: int = 20) -> list[JobError]:
        _get_job(pool, job_code)
        with pool.connection() as conn:
            rows = (
                conn.cursor(row_factory=dict_row)
                .execute(
                    "SELECT task_name, category, http_status, message, created_at FROM job_errors "
                    "WHERE job_code = %s ORDER BY created_at DESC LIMIT %s",
                    (job_code, min(max(limit, 1), 100)),
                )
                .fetchall()
            )
        return [JobError.model_validate(r) for r in rows]

    @app.post("/jobs/{job_code}/retry", response_model=Job)
    def retry_job(
        job_code: JobCode,
        pool: Pool,
        idempotency_key: Annotated[str, Header(min_length=8, max_length=128)],
    ) -> Job:
        # A retried request (network blip, agent loop) must not start a second round.
        # The key is reserved first: a concurrent request with the same key blocks on the
        # primary key until this transaction ends, then sees the stored response.
        with pool.connection() as conn, conn.transaction():
            reserved = conn.execute(
                "INSERT INTO idempotency_keys (key, job_code) VALUES (%s, %s) "
                "ON CONFLICT (key) DO NOTHING RETURNING key",
                (idempotency_key, job_code),
            ).fetchone()
            if reserved is None:
                stored_job, stored = conn.execute(
                    "SELECT job_code, response FROM idempotency_keys WHERE key = %s",
                    (idempotency_key,),
                ).fetchone() or (None, None)
                if stored_job != job_code:
                    raise HTTPException(422, "idempotency key was already used for another job")
                if stored is None:
                    raise HTTPException(409, "a request with this idempotency key is in progress")
                return Job.model_validate(stored)
            row = (
                conn.cursor(row_factory=dict_row)
                .execute(
                    f"""
                UPDATE jobs SET status = 'pending', retry_round = retry_round + 1, updated_at = now()
                WHERE job_code = %s AND status = 'failed'
                RETURNING {JOB_COLUMNS}
                """,  # noqa: S608
                    (job_code,),
                )
                .fetchone()
            )
            if row is None:
                current = _get_job(pool, job_code)
                raise HTTPException(
                    409, f"only failed jobs can be retried (status={current.status})"
                )
            job = Job.model_validate(row)
            conn.execute(
                "UPDATE idempotency_keys SET response = %s WHERE key = %s",
                (json.dumps(job.model_dump(mode="json")), idempotency_key),
            )
        return job

    @app.get("/runbooks/search", response_model=list[rag.Passage])
    def search_runbooks(q: str, pool: Pool, k: int = 3) -> list[rag.Passage]:
        if not q.strip() or len(q) > 500:
            raise HTTPException(422, "query must be 1-500 characters")
        return rag.search(pool, q, embedder, k=min(max(k, 1), 10))

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=8080)  # noqa: S104 - container entrypoint


if __name__ == "__main__":
    main()
