"""Shared fixtures.

Database tests run against a real Postgres (with pgvector):
- CI and docker compose set ``TEST_DATABASE_URL``;
- locally, an embedded throwaway Postgres (``pgserver``) is started, so no Docker is needed.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from psycopg_pool import ConnectionPool

from media_jobs_mcp.api_client import JobsApiClient
from media_jobs_mcp.config import Settings
from media_jobs_mcp.db import create_pool
from media_jobs_mcp.demo import reset_database
from media_jobs_mcp.jobs_api.app import create_app

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    if url := os.environ.get("TEST_DATABASE_URL"):
        yield url
        return
    pgserver = pytest.importorskip("pgserver")
    server = pgserver.get_server(tempfile.mkdtemp(), cleanup_mode="delete")
    yield server.get_uri()
    server.cleanup()


@pytest.fixture
def pool(database_url: str) -> Iterator[ConnectionPool]:
    pool = create_pool(database_url)
    reset_database(pool)
    yield pool
    pool.close()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        media_root=ROOT / "samples",
        service_token="test-service-token-0123456789",  # type: ignore[arg-type]
        api_base_url="http://jobs-api",
        api_rate_per_s=1000,
        api_burst=1000,
        failure_injection=True,
        client_rate_per_s=1000,
        client_max_concurrency=8,
        auth_mode="none",
    )


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
async def api(settings: Settings, pool: ConnectionPool) -> AsyncIterator[JobsApiClient]:
    """API client wired to the real FastAPI app in-process (ASGI), backed by real Postgres."""
    app = create_app(settings, pool=pool, runbooks_dir=ROOT / "runbooks")
    async with app.router.lifespan_context(app):
        client = JobsApiClient(settings, transport=httpx.ASGITransport(app=app), sleep=_no_sleep)
        yield client
        await client.aclose()
