"""OAuth 2.1 resource-server behaviour over the real Streamable HTTP transport.

A real uvicorn server is started on a free port; tokens are RS256 JWTs signed with a throwaway
key whose public JWKS the server trusts.
"""

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from media_jobs_mcp.api_client import JobsApiClient
from media_jobs_mcp.config import Settings
from media_jobs_mcp.devtokens import init_keys, mint
from media_jobs_mcp.server import build_server

ISSUER = "https://idp.example.test"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def keys(tmp_path_factory: pytest.TempPathFactory) -> Path:
    folder = tmp_path_factory.mktemp("keys")
    init_keys(folder)
    return folder


@pytest.fixture
def http_server(
    settings: Settings, api: JobsApiClient, keys: Path
) -> Iterator[tuple[str, Settings]]:
    port = _free_port()
    jwt_settings = settings.model_copy(
        update={
            "auth_mode": "jwt",
            "oidc_issuer": ISSUER,
            "oidc_audience": f"http://127.0.0.1:{port}/mcp",
            "jwks_file": keys / "jwks.json",
        }
    )
    app = build_server(jwt_settings, api).streamable_http_app(host="127.0.0.1")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", jwt_settings
    server.should_exit = True
    thread.join(timeout=5)


def _token(keys: Path, s: Settings, **overrides: Any) -> str:
    params: dict[str, Any] = {
        "issuer": s.oidc_issuer,
        "audience": s.oidc_audience,
        "scope": "jobs:read",
    }
    params.update(overrides)
    return mint(keys, **params)


async def _call(base: str, token: str, tool: str, args: dict[str, Any]) -> Any:
    headers = {"Authorization": f"Bearer {token}"}
    async with (
        httpx2.AsyncClient(headers=headers) as http,
        Client(streamable_http_client(f"{base}/mcp", http_client=http)) as client,
    ):
        return await client.call_tool(tool, args)


def test_missing_token_gets_401_with_resource_metadata(http_server: tuple[str, Settings]) -> None:
    base, _ = http_server
    resp = httpx.post(f"{base}/mcp", json={})
    assert resp.status_code == 401
    assert "resource_metadata" in resp.headers["www-authenticate"]

    meta = httpx.get(f"{base}/.well-known/oauth-protected-resource/mcp").json()
    assert meta["authorization_servers"] == [f"{ISSUER}/"] or meta["authorization_servers"] == [
        ISSUER
    ]


@pytest.mark.parametrize(
    "overrides",
    [
        {"audience": "http://some-other-server/mcp"},  # token minted for another resource
        {"issuer": "https://evil.example.test"},
        {"ttl_s": -120},  # expired beyond leeway
    ],
    ids=["wrong-audience", "wrong-issuer", "expired"],
)
def test_invalid_tokens_are_rejected(
    http_server: tuple[str, Settings], keys: Path, overrides: dict[str, Any]
) -> None:
    base, s = http_server
    token = _token(keys, s, **overrides)
    resp = httpx.post(f"{base}/mcp", json={}, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


async def test_read_scope_can_read(http_server: tuple[str, Settings], keys: Path) -> None:
    base, s = http_server
    result = await _call(base, _token(keys, s), "get_job_status", {"job_code": "MJ-0001"})
    assert not result.is_error
    assert result.structured_content["job_code"] == "MJ-0001"


async def test_read_scope_cannot_write(http_server: tuple[str, Settings], keys: Path) -> None:
    base, s = http_server
    result = await _call(
        base, _token(keys, s), "submit_job", {"source_id": "x-1", "title": "should not exist"}
    )
    assert result.is_error
    assert "missing scope: jobs:write" in result.content[0].text


async def test_write_scope_can_write(http_server: tuple[str, Settings], keys: Path) -> None:
    base, s = http_server
    token = _token(keys, s, scope="jobs:read jobs:write")
    result = await _call(base, token, "submit_job", {"source_id": "x-2", "title": "ok"})
    assert not result.is_error
    assert result.structured_content["created"] is True
