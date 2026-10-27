"""Self-contained environment: Postgres + jobs API + MCP server, all in one process.

Uses ``MJM_DATABASE_URL`` when it points at a reachable database (docker compose), otherwise
starts a throwaway embedded Postgres (``pgserver``, dev dependency). Used by the CLI demo and
the eval runner, so both work on a laptop with no Docker and no cloud account.
"""

from __future__ import annotations

import os
import secrets
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
from mcp.server.mcpserver import MCPServer
from psycopg_pool import ConnectionPool

from media_jobs_mcp.api_client import JobsApiClient
from media_jobs_mcp.config import Settings
from media_jobs_mcp.db import create_pool, migrate
from media_jobs_mcp.jobs_api.app import create_app
from media_jobs_mcp.seed import seed
from media_jobs_mcp.server import build_server

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class DemoEnv:
    server: MCPServer
    pool: ConnectionPool

    def reset(self) -> None:
        """Back to the seeded job data; the runbook index is kept."""
        with self.pool.connection() as conn:
            conn.execute(
                "TRUNCATE job_errors, jobs, idempotency_keys; ALTER SEQUENCE job_code_seq RESTART"
            )
        seed(self.pool)


def reset_database(pool: ConnectionPool) -> None:
    with pool.connection() as conn:
        conn.execute(
            "DROP TABLE IF EXISTS job_errors, jobs, idempotency_keys, runbook_chunks, "
            "schema_migrations CASCADE; DROP SEQUENCE IF EXISTS job_code_seq"
        )
    migrate(pool)
    seed(pool)


@asynccontextmanager
async def demo_environment() -> AsyncIterator[DemoEnv]:
    settings = Settings(
        media_root=ROOT / "samples",
        api_base_url="http://jobs-api",
        service_token=secrets.token_urlsafe(32),  # type: ignore[arg-type]
        auth_mode="none",
    )
    embedded = None
    url = os.environ.get("MJM_DATABASE_URL")
    if not url:
        import pgserver  # dev dependency

        embedded = pgserver.get_server(  # type: ignore[attr-defined]
            tempfile.mkdtemp(prefix="mjm-"), cleanup_mode="delete"
        )
        url = embedded.get_uri()

    pool = create_pool(url)
    reset_database(pool)
    app = create_app(settings, pool=pool, runbooks_dir=ROOT / "runbooks")
    try:
        async with app.router.lifespan_context(app):
            api = JobsApiClient(settings, transport=httpx.ASGITransport(app=app))
            try:
                yield DemoEnv(server=build_server(settings, api), pool=pool)
            finally:
                await api.aclose()
    finally:
        pool.close()
        if embedded:
            embedded.cleanup()
