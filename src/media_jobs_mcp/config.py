"""Runtime configuration. Every value comes from the environment; nothing is hardcoded."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MJM_", env_file=".env", extra="ignore")

    # --- media probing ---
    media_root: Path = Path("samples")
    """Only files under this directory can be probed (path-traversal guard)."""
    ffprobe_bin: str = "ffprobe"
    ffprobe_timeout_s: float = 30.0

    # --- jobs API ---
    database_url: SecretStr | None = None
    """Required by the jobs API (it owns the database); the MCP server never connects to it."""
    api_base_url: str = "http://localhost:8080"
    service_token: SecretStr = Field(min_length=24)
    """Credential the MCP server uses to call the jobs API. Never the end user's token.
    Required, no default: a deployment without it fails at startup instead of running open."""
    api_rate_per_s: float = 5.0
    api_burst: int = 10
    seed_demo_data: bool = False
    """Load the demo jobs on startup (local stack only)."""
    failure_injection: bool = False
    """Lets demos/tests force 429/503 responses. Must stay off outside local environments."""

    # --- MCP server auth ---
    auth_mode: Literal["none", "jwt"] = "none"
    """`none` is only for the stdio transport (local, single user). HTTP requires `jwt`."""
    oidc_issuer: str = "http://localhost:9000"
    oidc_audience: str = "http://localhost:8000/mcp"
    jwks_url: str | None = None
    jwks_file: Path | None = None
    """Local JWKS for development; production points `jwks_url` at the IdP (Auth0, Keycloak...)."""

    # --- client resilience ---
    client_max_concurrency: int = 4
    client_rate_per_s: float = 4.0
    client_max_retries: int = 4
    http_timeout_s: float = 10.0

    @field_validator("service_token")
    @classmethod
    def _not_a_placeholder(cls, v: SecretStr) -> SecretStr:
        if v.get_secret_value().lower().startswith(("change-me", "changeme")):
            raise ValueError("MJM_SERVICE_TOKEN still has the placeholder value from .env.example")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # required fields come from the environment
