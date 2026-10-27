"""Client retry policy and rate limiting, with no real waiting (clock and sleep are faked)."""

import httpx
import pytest
import respx

from media_jobs_mcp.api_client import ApiError, JobsApiClient
from media_jobs_mcp.config import Settings
from media_jobs_mcp.ratelimit import TokenBucket, retry_after_header

JOB = {
    "job_code": "MJ-0001",
    "source_id": "s",
    "title": "t",
    "status": "failed",
    "retry_round": 0,
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
}


class FakeSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def make_client(settings: Settings, sleep: FakeSleep) -> JobsApiClient:
    return JobsApiClient(settings, sleep=sleep)


@respx.mock
async def test_429_waits_for_retry_after_then_succeeds(settings: Settings) -> None:
    route = respx.get("http://jobs-api/jobs/MJ-0001").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json=JOB),
        ]
    )
    sleep = FakeSleep()
    job = await make_client(settings, sleep).get_job("MJ-0001")
    assert job.job_code == "MJ-0001"
    assert route.call_count == 2
    assert 7.0 in sleep.calls


@respx.mock
async def test_503_uses_short_backoff(settings: Settings) -> None:
    respx.get("http://jobs-api/jobs/MJ-0001").mock(
        side_effect=[httpx.Response(503), httpx.Response(503), httpx.Response(200, json=JOB)]
    )
    sleep = FakeSleep()
    await make_client(settings, sleep).get_job("MJ-0001")
    assert len(sleep.calls) == 2
    assert all(s <= 5 for s in sleep.calls)


@respx.mock
async def test_422_is_never_retried(settings: Settings) -> None:
    route = respx.post("http://jobs-api/jobs").mock(
        return_value=httpx.Response(422, json={"detail": "bad"})
    )
    with pytest.raises(ApiError) as exc:
        await make_client(settings, FakeSleep()).submit_job("x", "y")
    assert exc.value.status == 422
    assert route.call_count == 1


@respx.mock
async def test_read_timeout_is_not_retried(settings: Settings) -> None:
    route = respx.get("http://jobs-api/jobs/MJ-0001").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ApiError, match="not retried"):
        await make_client(settings, FakeSleep()).get_job("MJ-0001")
    assert route.call_count == 1


@respx.mock
async def test_gives_up_after_max_retries(settings: Settings) -> None:
    route = respx.get("http://jobs-api/jobs/MJ-0001").mock(return_value=httpx.Response(503))
    with pytest.raises(ApiError) as exc:
        await make_client(settings, FakeSleep()).get_job("MJ-0001")
    assert exc.value.status == 503
    assert route.call_count == settings.client_max_retries + 1


@respx.mock
async def test_retry_reuses_the_same_idempotency_key(settings: Settings) -> None:
    route = respx.post("http://jobs-api/jobs/MJ-0001/retry").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json=JOB)]
    )
    await make_client(settings, FakeSleep()).retry_job("MJ-0001")
    keys = {call.request.headers["Idempotency-Key"] for call in route.calls}
    assert len(keys) == 1


def test_token_bucket_refills_over_time() -> None:
    now = [0.0]
    bucket = TokenBucket(rate_per_s=2, capacity=2, clock=lambda: now[0])
    assert bucket.try_acquire() == 0
    assert bucket.try_acquire() == 0
    assert bucket.try_acquire() == pytest.approx(0.5)
    now[0] += 0.5
    assert bucket.try_acquire() == 0


def test_retry_after_rounds_up() -> None:
    assert retry_after_header(0.2) == "1"
    assert retry_after_header(2.1) == "3"


async def test_server_side_rate_limit_returns_429_with_retry_after(
    settings: Settings, pool: object
) -> None:
    from media_jobs_mcp.jobs_api.app import create_app

    limited = settings.model_copy(update={"api_rate_per_s": 0.001, "api_burst": 2})
    app = create_app(limited, pool=pool, clock=lambda: 0.0, runbooks_dir=None)  # type: ignore[arg-type]
    headers = {"Authorization": "Bearer test-service-token-0123456789", "X-Client-Id": "burst"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http,
    ):
        codes = [(await http.get("/jobs/MJ-0001", headers=headers)) for _ in range(3)]
    assert [r.status_code for r in codes] == [200, 200, 429]
    assert codes[2].headers["Retry-After"].isdigit()


async def test_api_rejects_wrong_service_token(settings: Settings, pool: object) -> None:
    from media_jobs_mcp.jobs_api.app import create_app

    app = create_app(settings, pool=pool, runbooks_dir=None)  # type: ignore[arg-type]
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http,
    ):
        resp = await http.get("/jobs/MJ-0001", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


@respx.mock
async def test_non_json_error_page_becomes_clean_api_error(settings: Settings) -> None:
    respx.get("http://jobs-api/jobs/MJ-0001").mock(
        return_value=httpx.Response(400, text="<html>Bad Gateway</html>")
    )
    with pytest.raises(ApiError) as exc:
        await make_client(settings, FakeSleep()).get_job("MJ-0001")
    assert exc.value.status == 400
    assert "<html>" not in str(exc.value)


def test_bucket_registry_is_bounded() -> None:
    from media_jobs_mcp.ratelimit import BucketRegistry

    registry = BucketRegistry(1, 1, clock=lambda: 0.0, max_keys=3)
    for i in range(10):
        registry.get(f"client-{i}")
    assert len(registry) == 3


def test_placeholder_service_token_is_rejected() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(service_token="change-me-please-0123456789")  # type: ignore[arg-type]
