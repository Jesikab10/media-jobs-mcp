"""Resilient async client for the jobs API.

Retry policy (deliberately selective):

| Response            | Action                                                        |
|---------------------|---------------------------------------------------------------|
| 429                 | wait ``Retry-After`` (or long backoff) and retry              |
| 502 / 503 / 504     | short exponential backoff with jitter and retry               |
| connect error       | short backoff and retry (request never reached the server)    |
| other 4xx           | no retry: the request itself is wrong                         |
| read timeout        | no retry: the server may still be working; retrying a write   |
|                     | could duplicate work. Idempotency keys make writes safe to    |
|                     | replay manually.                                              |

Outgoing calls are also throttled client-side (token bucket + concurrency cap), so the
client avoids most 429s instead of only reacting to them.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import httpx

from media_jobs_mcp.config import Settings
from media_jobs_mcp.models import Job, JobError, JobSubmitResult
from media_jobs_mcp.rag import Passage
from media_jobs_mcp.ratelimit import TokenBucket

RETRYABLE_SHORT = {502, 503, 504}


class ApiError(Exception):
    """Error with a message that is safe to surface to an agent (no URLs, no secrets)."""

    def __init__(self, status: int | None, message: str):
        super().__init__(message)
        self.status = status


class JobsApiClient:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        client_id: str = "media-jobs-mcp",
    ):
        self._settings = settings
        self._sleep = sleep
        self._bucket = TokenBucket(settings.client_rate_per_s, settings.client_max_concurrency)
        self._semaphore = asyncio.Semaphore(settings.client_max_concurrency)
        self._http = httpx.AsyncClient(
            base_url=settings.api_base_url,
            timeout=settings.http_timeout_s,
            transport=transport,
            headers={
                "Authorization": f"Bearer {settings.service_token.get_secret_value()}",
                "X-Client-Id": client_id,
            },
        )
        self.attempts = 0
        """Total HTTP attempts made; exposed for tests and metrics."""

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _throttle(self) -> None:
        while (wait := self._bucket.try_acquire()) > 0:
            await self._sleep(wait)

    def _backoff(self, attempt: int, base: float, cap: float) -> float:
        jitter = 0.5 + random.random() / 2  # noqa: S311 - jitter, not cryptography
        return float(min(cap, base * 2**attempt) * jitter)

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        for attempt in range(self._settings.client_max_retries + 1):
            await self._throttle()
            last = attempt == self._settings.client_max_retries
            try:
                async with self._semaphore:
                    self.attempts += 1
                    resp = await self._http.request(method, path, **kwargs)
            except httpx.ConnectError as exc:
                if last:
                    raise ApiError(None, "jobs API is unreachable") from exc
                await self._sleep(self._backoff(attempt, 0.5, 5))
                continue
            except httpx.TimeoutException as exc:
                raise ApiError(
                    None, "jobs API timed out; not retried to avoid duplicate work"
                ) from exc

            if resp.status_code < 400:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise ApiError(resp.status_code, "jobs API returned an invalid body") from exc
            if resp.status_code == 429 and not last:
                retry_after = resp.headers.get("Retry-After")
                wait = (
                    float(retry_after)
                    if retry_after and retry_after.isdigit()
                    else self._backoff(attempt, 2, 30)
                )
                await self._sleep(wait)
                continue
            if resp.status_code in RETRYABLE_SHORT and not last:
                await self._sleep(self._backoff(attempt, 0.5, 5))
                continue
            try:
                detail = resp.json().get("detail", "error")
            except (ValueError, AttributeError):
                detail = "error"  # non-JSON error page from a proxy or load balancer
            raise ApiError(resp.status_code, f"jobs API returned {resp.status_code}: {detail}")
        raise AssertionError("unreachable")  # pragma: no cover

    async def submit_job(self, source_id: str, title: str) -> JobSubmitResult:
        data = await self._request("POST", "/jobs", json={"source_id": source_id, "title": title})
        return JobSubmitResult.model_validate(data)

    async def get_job(self, job_code: str) -> Job:
        return Job.model_validate(await self._request("GET", f"/jobs/{quote(job_code, safe='')}"))

    async def list_errors(self, job_code: str, limit: int = 20) -> list[JobError]:
        data = await self._request(
            "GET", f"/jobs/{quote(job_code, safe='')}/errors", params={"limit": limit}
        )
        return [JobError.model_validate(e) for e in data]

    async def retry_job(self, job_code: str, idempotency_key: str | None = None) -> Job:
        # The same key is reused across this method's internal retries, so a 503 after the
        # server committed cannot start a second round.
        key = idempotency_key or str(uuid.uuid4())
        data = await self._request(
            "POST", f"/jobs/{quote(job_code, safe='')}/retry", headers={"Idempotency-Key": key}
        )
        return Job.model_validate(data)

    async def search_runbooks(self, query: str, k: int = 3) -> list[Passage]:
        data = await self._request("GET", "/runbooks/search", params={"q": query, "k": k})
        return [Passage.model_validate(p) for p in data]
