import pytest

from media_jobs_mcp.api_client import ApiError, JobsApiClient


async def test_submit_is_idempotent_per_source_id(api: JobsApiClient) -> None:
    first = await api.submit_job("upstream-42", "New spot")
    replay = await api.submit_job("upstream-42", "New spot")
    assert first.created is True
    assert replay.created is False
    assert replay.job.job_code == first.job.job_code


async def test_retry_only_failed_jobs(api: JobsApiClient) -> None:
    job = await api.retry_job("MJ-0001", "key-retry-0001")
    assert (job.status, job.retry_round) == ("pending", 1)

    with pytest.raises(ApiError) as exc:
        await api.retry_job("MJ-0005", "key-retry-0005")
    assert exc.value.status == 409


async def test_retry_replay_with_same_key_does_not_start_second_round(api: JobsApiClient) -> None:
    first = await api.retry_job("MJ-0001", "same-key-123")
    replay = await api.retry_job("MJ-0001", "same-key-123")
    assert replay.retry_round == first.retry_round == 1


async def test_unknown_job_is_404_without_retry(api: JobsApiClient) -> None:
    with pytest.raises(ApiError) as exc:
        await api.get_job("MJ-9999")
    assert exc.value.status == 404
    assert api.attempts == 1


async def test_runbook_search_returns_cited_passages(api: JobsApiClient) -> None:
    passages = await api.search_runbooks("encoder returned 503 service unavailable")
    assert passages
    assert passages[0].source.startswith("transient-errors.md#")


async def test_idempotency_key_cannot_be_reused_for_another_job(api: JobsApiClient) -> None:
    await api.retry_job("MJ-0001", "shared-key-1")
    with pytest.raises(ApiError) as exc:
        await api.retry_job("MJ-0004", "shared-key-1")
    assert exc.value.status == 422
    job = await api.get_job("MJ-0004")
    assert job.status == "failed"  # untouched


async def test_failed_retry_releases_the_idempotency_key(api: JobsApiClient) -> None:
    with pytest.raises(ApiError):
        await api.retry_job("MJ-0005", "key-for-completed")  # 409: not failed
    # The rejected request must not leave the key reserved.
    with pytest.raises(ApiError) as exc:
        await api.retry_job("MJ-0005", "key-for-completed")
    assert exc.value.status == 409


async def test_job_code_cannot_escape_its_route(api: JobsApiClient) -> None:
    with pytest.raises(ApiError) as exc:
        await api.get_job("../runbooks/search")
    assert exc.value.status in {404, 422}


async def test_runbook_search_matches_any_term(api: JobsApiClient) -> None:
    # "encoder" and "503" never appear in the same runbook section. With AND semantics the
    # lexical side returned nothing; with OR it must surface the transient-errors runbook.
    passages = await api.search_runbooks("encoder 503")
    assert any(p.source.startswith("transient-errors.md#") for p in passages)
