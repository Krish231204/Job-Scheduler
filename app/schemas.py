from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field, model_validator

from app.models import (
    CheckOutcome,
    JobStatus,
    JobType,
    OrgRole,
    RetryStrategy,
    WatchAlertKind,
    WatchKind,
    WatchState,
    WorkerStatus,
)


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: str = ""


class UserOut(BaseModel):
    id: int
    email: str
    full_name: str
    created_at: datetime

    model_config = {"from_attributes": True}


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


# --------------------------------------------------------------------------
# Organizations / Projects
# --------------------------------------------------------------------------

class OrganizationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class OrganizationOut(BaseModel):
    id: int
    name: str
    created_at: datetime
    role: OrgRole | None = None

    model_config = {"from_attributes": True}


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str = ""


class ProjectOut(BaseModel):
    id: int
    organization_id: int
    name: str
    description: str
    created_at: datetime

    model_config = {"from_attributes": True}


# --------------------------------------------------------------------------
# Retry policy / Queues
# --------------------------------------------------------------------------

class RetryPolicyIn(BaseModel):
    strategy: RetryStrategy = RetryStrategy.EXPONENTIAL
    max_retries: int = Field(default=5, ge=0, le=50)
    base_delay_seconds: float = Field(default=2.0, ge=0)
    multiplier: float = Field(default=2.0, ge=1)
    max_delay_seconds: float = Field(default=3600.0, ge=0)


class RetryPolicyOut(RetryPolicyIn):
    model_config = {"from_attributes": True}


class QueueCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    priority: int = 0
    max_concurrency: int = Field(default=4, ge=1, le=1000)
    retry_policy: RetryPolicyIn = Field(default_factory=RetryPolicyIn)


class QueueUpdate(BaseModel):
    priority: int | None = None
    max_concurrency: int | None = Field(default=None, ge=1, le=1000)
    is_paused: bool | None = None
    retry_policy: RetryPolicyIn | None = None


class QueueOut(BaseModel):
    id: int
    project_id: int
    name: str
    priority: int
    max_concurrency: int
    is_paused: bool
    created_at: datetime
    updated_at: datetime
    retry_policy: RetryPolicyOut | None = None

    model_config = {"from_attributes": True}


class QueueStats(BaseModel):
    queue_id: int
    queued: int
    scheduled: int
    blocked: int = 0
    claimed: int
    running: int
    completed: int
    failed: int
    dead_letter: int
    cancelled: int
    avg_duration_ms: float | None = None
    throughput_last_hour: int = 0
    # Current-rate metrics over the last RATE_WINDOW_SECONDS (see
    # app/services/stats.py): completions per second, and latency
    # percentiles of successful execution durations.
    jobs_per_second: float = 0.0
    p50_ms: float | None = None
    p95_ms: float | None = None
    p99_ms: float | None = None


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------

class JobCreate(BaseModel):
    """Unified job creation payload. `job_type` determines which fields are required."""
    name: str = Field(min_length=1, max_length=255)
    job_type: JobType = JobType.IMMEDIATE
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int | None = None
    idempotency_key: str | None = None

    # delayed
    delay_seconds: float | None = Field(default=None, ge=0)
    # scheduled (one-off future timestamp)
    run_at: datetime | None = None
    # recurring
    cron_expression: str | None = None
    # batch
    batch_items: list[dict[str, Any]] | None = None

    max_retries: int | None = Field(default=None, ge=0, le=50)
    retry_strategy: RetryStrategy | None = None

    # DAG: ids of jobs (same queue) that must COMPLETE before this one runs.
    depends_on: list[int] | None = Field(default=None, max_length=50)
    # Per-execution wall-clock limit; the worker cancels the handler and
    # fails the attempt into the normal retry path when exceeded.
    timeout_seconds: float | None = Field(default=None, gt=0, le=86400)

    @model_validator(mode="after")
    def validate_type_fields(self) -> "JobCreate":
        if self.job_type == JobType.DELAYED and self.delay_seconds is None:
            raise ValueError("delay_seconds is required for delayed jobs")
        if self.job_type == JobType.SCHEDULED and self.run_at is None:
            raise ValueError("run_at is required for scheduled jobs")
        if self.job_type == JobType.RECURRING and not self.cron_expression:
            raise ValueError("cron_expression is required for recurring jobs")
        if self.job_type == JobType.BATCH and not self.batch_items:
            raise ValueError("batch_items (non-empty list) is required for batch jobs")
        if self.job_type == JobType.BATCH and self.depends_on:
            raise ValueError("depends_on is not supported for batch jobs")
        return self


class JobOut(BaseModel):
    id: int
    queue_id: int
    scheduled_job_id: int | None
    batch_id: str | None
    name: str
    job_type: JobType
    status: JobStatus
    payload: dict[str, Any]
    priority: int | None
    run_at: datetime
    timeout_seconds: float | None = None
    attempt_count: int
    next_retry_at: datetime | None
    claimed_by: int | None
    claimed_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class JobExecutionOut(BaseModel):
    id: int
    job_id: int
    attempt_number: int
    worker_id: int | None
    status: str
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    result: dict[str, Any] | None
    error: str | None

    model_config = {"from_attributes": True}


class JobLogOut(BaseModel):
    id: int
    job_id: int
    execution_id: int | None
    level: str
    message: str
    timestamp: datetime

    model_config = {"from_attributes": True}


class JobDetailOut(JobOut):
    executions: list[JobExecutionOut] = []
    logs: list[JobLogOut] = []


class PaginatedJobs(BaseModel):
    items: list[JobOut]
    total: int
    page: int
    page_size: int


class AISummaryOut(BaseModel):
    summary: str
    cached: bool


# --------------------------------------------------------------------------
# Scheduled job definitions
# --------------------------------------------------------------------------

class ScheduledJobCreate(BaseModel):
    name: str
    job_name: str
    payload_template: dict[str, Any] = Field(default_factory=dict)
    cron_expression: str | None = None
    run_at: datetime | None = None
    is_recurring: bool = False

    @model_validator(mode="after")
    def validate_trigger(self) -> "ScheduledJobCreate":
        if self.is_recurring and not self.cron_expression:
            raise ValueError("cron_expression required when is_recurring=true")
        if not self.is_recurring and self.run_at is None:
            raise ValueError("run_at required when is_recurring=false")
        return self


class ScheduledJobOut(BaseModel):
    id: int
    queue_id: int
    name: str
    job_name: str
    cron_expression: str | None
    run_at: datetime | None
    is_recurring: bool
    is_active: bool
    last_run_at: datetime | None
    next_run_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


# --------------------------------------------------------------------------
# Watches
# --------------------------------------------------------------------------

class WatchCreate(BaseModel):
    organization_id: int
    name: str = Field(min_length=1, max_length=255)
    url: str = Field(min_length=1, max_length=2000)
    kind: WatchKind
    keyword: str | None = Field(default=None, max_length=255)
    interval_seconds: int = Field(default=300, ge=60, le=86400)
    webhook_url: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_kind_fields(self) -> "WatchCreate":
        if self.kind == WatchKind.KEYWORD and not (self.keyword and self.keyword.strip()):
            raise ValueError("keyword is required for keyword watches")
        return self


class WatchOut(BaseModel):
    id: int
    organization_id: int
    queue_id: int
    name: str
    url: str
    kind: WatchKind
    keyword: str | None
    interval_seconds: int
    webhook_url: str | None
    is_active: bool
    state: WatchState
    consecutive_failures: int
    last_check_at: datetime | None
    next_check_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class WatchCheckOut(BaseModel):
    id: int
    watch_id: int
    started_at: datetime
    latency_ms: int | None
    http_status: int | None
    outcome: CheckOutcome | None
    keyword_found: bool | None
    detail: str | None

    model_config = {"from_attributes": True}


class WatchAlertOut(BaseModel):
    id: int
    watch_id: int
    check_id: int | None
    kind: WatchAlertKind
    message: str
    delivered: bool
    delivery_detail: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class WatchStats(BaseModel):
    watch_id: int
    total_checks: int
    checks_24h: int
    ok_pct_24h: float | None = None
    p50_latency_ms: float | None = None
    p95_latency_ms: float | None = None
    last_latency_ms: int | None = None


# --------------------------------------------------------------------------
# Workers
# --------------------------------------------------------------------------

class WorkerOut(BaseModel):
    id: int
    name: str
    hostname: str
    pid: int
    status: WorkerStatus
    concurrency: int
    started_at: datetime
    last_seen_at: datetime

    model_config = {"from_attributes": True}


class WorkerHeartbeatIn(BaseModel):
    active_job_count: int = 0
    metrics: dict[str, Any] = Field(default_factory=dict)
