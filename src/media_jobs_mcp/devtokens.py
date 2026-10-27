"""Local development token issuer. NOT an authorization server.

Generates an RSA key pair, writes the public JWKS, and mints short-lived access tokens so the
OAuth-protected HTTP transport can be exercised without an external IdP. In any shared
environment, point ``MJM_JWKS_URL`` at a real IdP (Auth0, Keycloak, Okta) instead.

    python -m media_jobs_mcp.devtokens init  --dir .dev-keys
    python -m media_jobs_mcp.devtokens mint  --dir .dev-keys --scope "jobs:read jobs:write"
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

KID = "dev-key-1"


def init_keys(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (folder / "private.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public_jwk.update({"kid": KID, "use": "sig", "alg": "RS256"})
    jwks_path = folder / "jwks.json"
    jwks_path.write_text(json.dumps({"keys": [public_jwk]}, indent=2), encoding="utf-8")
    return jwks_path


def mint(
    folder: Path,
    *,
    issuer: str,
    audience: str,
    scope: str,
    subject: str = "dev-user",
    ttl_s: int = 900,
    now: int | None = None,
) -> str:
    now = int(time.time()) if now is None else now
    claims = {
        "iss": issuer,
        "aud": audience,
        "sub": subject,
        "scope": scope,
        "iat": now,
        "nbf": now,
        "exp": now + ttl_s,
        "jti": str(uuid.uuid4()),
    }
    private_key = (folder / "private.pem").read_bytes()
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": KID})


def main() -> None:
    from media_jobs_mcp.config import get_settings

    parser = argparse.ArgumentParser(prog="devtokens")
    parser.add_argument("command", choices=["init", "mint"])
    parser.add_argument("--dir", type=Path, default=Path(".dev-keys"))
    parser.add_argument("--scope", default="jobs:read")
    parser.add_argument("--subject", default="dev-user")
    args = parser.parse_args()

    if args.command == "init":
        print(init_keys(args.dir))
    else:
        s = get_settings()
        print(
            mint(
                args.dir,
                issuer=s.oidc_issuer,
                audience=s.oidc_audience,
                scope=args.scope,
                subject=args.subject,
            )
        )


if __name__ == "__main__":
    main()
