"""Wire models shared by the jobs API, its client and the MCP tools."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

JOB_CODE_PATTERN = r"^MJ-\d{4,8}$"
JobCode = Annotated[str, Field(pattern=JOB_CODE_PATTERN, description='e.g. "MJ-0001"')]

JobStatus = Literal["pending", "processing", "completed", "failed", "ignored"]
ErrorCategory = Literal["transient", "input", "technical", "unknown"]


class JobCreate(BaseModel):
    source_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    title: str = Field(min_length=1, max_length=200)


class Job(BaseModel):
    job_code: str
    source_id: str
    title: str
    status: JobStatus
    retry_round: int
    created_at: datetime
    updated_at: datetime


class JobSubmitResult(BaseModel):
    job: Job
    created: bool
    """False when the source_id already existed: the call was a safe replay, nothing was duplicated."""


class JobError(BaseModel):
    task_name: str
    category: ErrorCategory
    http_status: int | None
    message: str
    created_at: datetime
