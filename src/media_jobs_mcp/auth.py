"""OAuth 2.1 resource-server side of the MCP server.

The MCP server never issues tokens. It validates bearer tokens minted by an external
authorization server (Auth0, Keycloak, Okta, ...) and maps scopes to tools:

- signature checked against the issuer's JWKS (allow-listed algorithms only)
- ``iss``, ``aud`` (this server's resource URL, RFC 8707), ``exp`` and ``nbf`` enforced
- each tool declares the scope it needs; read and write are separate

The SDK serves the Protected Resource Metadata document (RFC 9728) and answers missing or
invalid tokens with ``401`` + ``WWW-Authenticate``, so clients can discover the issuer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jwt
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken

from media_jobs_mcp.config import Settings

ALLOWED_ALGORITHMS = ["RS256", "ES256"]
SCOPE_READ = "jobs:read"
SCOPE_WRITE = "jobs:write"


class ScopeError(PermissionError):
    pass


class JwtTokenVerifier:
    """Implements the SDK's ``TokenVerifier`` protocol."""

    def __init__(self, settings: Settings, leeway_s: int = 30):
        self._issuer = settings.oidc_issuer
        self._audience = settings.oidc_audience
        self._leeway = leeway_s
        self._jwks_client: jwt.PyJWKClient | None = None
        self._local_keys: dict[str, Any] = {}
        if settings.jwks_url:
            self._jwks_client = jwt.PyJWKClient(settings.jwks_url, cache_keys=True, lifespan=300)
        elif settings.jwks_file:
            self._local_keys = _load_jwks(settings.jwks_file)
        else:
            raise ValueError("jwt auth needs MJM_JWKS_URL or MJM_JWKS_FILE")

    def _signing_key(self, token: str) -> Any:
        if self._jwks_client:
            return self._jwks_client.get_signing_key_from_jwt(token).key
        kid = jwt.get_unverified_header(token).get("kid")
        if kid not in self._local_keys:
            raise jwt.InvalidKeyError("unknown kid")
        return self._local_keys[kid]

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = jwt.decode(
                token,
                key=self._signing_key(token),
                algorithms=ALLOWED_ALGORITHMS,
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except (jwt.PyJWTError, jwt.PyJWKClientError):
            return None  # the SDK turns this into 401 + WWW-Authenticate
        return AccessToken(
            token=token,
            client_id=str(claims.get("azp") or claims.get("client_id") or claims["sub"]),
            scopes=str(claims.get("scope", "")).split(),
            expires_at=int(claims["exp"]),
            resource=self._audience,
            subject=str(claims["sub"]),
            claims={"iss": claims["iss"]},
        )


def _load_jwks(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k["kid"]: jwt.PyJWK(k).key for k in data["keys"]}


def require_scope(scope: str, settings: Settings) -> str:
    """Return the caller's subject, or raise if the token lacks ``scope``.

    With ``auth_mode=none`` (stdio only: a local process acting for the user who started it)
    the caller is the local operator.
    """
    if settings.auth_mode == "none":
        return "local-operator"
    token = get_access_token()
    if token is None:
        raise ScopeError("authentication required")
    if scope not in token.scopes:
        raise ScopeError(f"missing scope: {scope}")
    return token.subject or token.client_id
