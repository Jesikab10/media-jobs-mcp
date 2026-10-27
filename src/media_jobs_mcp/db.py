"""Database access: connection pool and forward-only SQL migrations."""

from __future__ import annotations

from importlib import resources

from psycopg_pool import ConnectionPool

MIGRATIONS_PACKAGE = "media_jobs_mcp.migrations"


def create_pool(database_url: str) -> ConnectionPool:
    pool = ConnectionPool(database_url, min_size=1, max_size=10, open=False)
    pool.open(wait=True, timeout=30)
    return pool


def migrate(pool: ConnectionPool) -> list[str]:
    """Apply pending ``NNN_*.sql`` files in order. Re-running is a no-op."""
    files = sorted(
        (f for f in resources.files(MIGRATIONS_PACKAGE).iterdir() if f.name.endswith(".sql")),
        key=lambda f: f.name,
    )
    applied: list[str] = []
    with pool.connection() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}
        for f in files:
            if f.name in done:
                continue
            with conn.transaction():
                conn.execute(f.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (f.name,))
            applied.append(f.name)
    return applied
