"""
SQLAlchemy ORM models for the Codity distributed job scheduler.

Entity list (per assignment spec): Users, Organizations, Projects, Queues,
Jobs, Job Executions, Retry Policies, Workers, Worker Heartbeats, Job Logs,
Scheduled Jobs, Dead Letter Queue entries.

Design notes:
- Integer surrogate PKs everywhere for compact indexes and cheap FK joins.
- All FKs that represent strong ownership cascade on delete
  (org -> project -> queue -> job -> job_execution/job_log).
- Job claiming relies on a composite index on (queue_id, status, priority,
  run_at) plus `SELECT ... FOR UPDATE SKIP LOCKED` (see services/job_service.py)
  to guarantee at-most-one-worker-claims-a-job under concurrent polling.
- JSONB columns hold flexible payload/result/metadata without extra tables.
"""
import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------

class OrgRole(str, enum.Enum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class JobType(str, enum.Enum):
    IMMEDIATE = "immediate"
    DELAYED = "delayed"
    SCHEDULED = "scheduled"
    RECURRING = "recurring"
    BATCH = "batch"


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    SCHEDULED = "scheduled"
    CLAIMED = "claimed"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYING = "retrying"
    DEAD_LETTER = "dead_letter"
    CANCELLED = "cancelled"


class RetryStrategy(str, enum.Enum):
    FIXED = "fixed"
    LINEAR = "linear"
    EXPONENTIAL = "exponential"


class WorkerStatus(str, enum.Enum):
    ONLINE = "online"
    DRAINING = "draining"
    OFFLINE = "offline"


class ExecutionStatus(str, enum.Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class LogLevel(str, enum.Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


def _enum_values(enum_cls):
    """SQLAlchemy's Enum(), given a Python enum class, binds/reads using the
    member *name* (e.g. "RETRYING") by default -- not `.value` ("retrying").
    Every enum column below stores lowercase string values (set by the
    Alembic migration), so every one of them needs `values_callable=_enum_values`
    or the ORM sends the wrong text and Postgres rejects it with
    'invalid input value for enum ...'. Passed as a plain function (not a
    lambda) so it's easy to spot when grepping for this pattern.
    """
    return [member.value for member in enum_cls]


# --------------------------------------------------------------------------
# Users / Organizations / Projects
# --------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    memberships: Mapped[list["OrganizationMember"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    members: Mapped[list["OrganizationMember"]] = relationship(back_populates="organization", cascade="all, delete-orphan")
    projects: Mapped[list["Project"]] = relationship(back_populates="organization", cascade="all, delete-orphan")


class OrganizationMember(Base):
    __tablename__ = "organization_members"
    __table_args__ = (UniqueConstraint("organization_id", "user_id", name="uq_org_member"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[OrgRole] = mapped_column(
        Enum(OrgRole, name="org_role", values_callable=_enum_values), default=OrgRole.MEMBER, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    organization: Mapped["Organization"] = relationship(back_populates="members")
    user: Mapped["User"] = relationship(back_populates="memberships")


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("organization_id", "name", name="uq_project_name_per_org"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    organization: Mapped["Organization"] = relationship(back_populates="projects")
    queues: Mapped[list["Queue"]] = relationship(back_populates="project", cascade="all, delete-orphan")


# --------------------------------------------------------------------------
# Queues / Retry policies
# --------------------------------------------------------------------------

class RetryPolicy(Base):
    """Reusable retry policy attached to a queue; jobs may override fields individually."""
    __tablename__ = "retry_policies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    queue_id: Mapped[int] = mapped_column(ForeignKey("queues.id", ondelete="CASCADE"), unique=True, index=True)
    strategy: Mapped[RetryStrategy] = mapped_column(
        Enum(RetryStrategy, name="retry_strategy", values_callable=_enum_values), default=RetryStrategy.EXPONENTIAL
    )
    max_retries: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    base_delay_seconds: Mapped[float] = mapped_column(Float, default=2.0, nullable=False)
    multiplier: Mapped[float] = mapped_column(Float, default=2.0, nullable=False)  # used by exponential/linear
    max_delay_seconds: Mapped[float] = mapped_column(Float, default=3600.0, nullable=False)

    queue: Mapped["Queue"] = relationship(back_populates="retry_policy")

    __table_args__ = (
        CheckConstraint("max_retries >= 0", name="ck_retry_max_retries_nonneg"),
        CheckConstraint("base_delay_seconds >= 0", name="ck_retry_base_delay_nonneg"),
    )


class Queue(Base):
    __tablename__ = "queues"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_queue_name_per_project"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # higher = served first
    max_concurrency: Mapped[int] = mapped_column(Integer, default=4, nullable=False)
    is_paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    project: Mapped["Project"] = relationship(back_populates="queues")
    retry_policy: Mapped["RetryPolicy | None"] = relationship(back_populates="queue", uselist=False, cascade="all, delete-orphan")
    jobs: Mapped[list["Job"]] = relationship(back_populates="queue", cascade="all, delete-orphan")
    scheduled_jobs: Mapped[list["ScheduledJob"]] = relationship(back_populates="queue", cascade="all, delete-orphan")


# --------------------------------------------------------------------------
# Scheduled job definitions (cron templates / future one-off definitions)
# --------------------------------------------------------------------------

class ScheduledJob(Base):
    """A definition that periodically (cron) or once (run_at) materializes a Job row."""
    __tablename__ = "scheduled_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    queue_id: Mapped[int] = mapped_column(ForeignKey("queues.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    job_name: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_template: Mapped[dict] = mapped_column(JSON, default=dict)
    cron_expression: Mapped[str | None] = mapped_column(String(120), nullable=True)
    run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_recurring: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    queue: Mapped["Queue"] = relationship(back_populates="scheduled_jobs")
    jobs: Mapped[list["Job"]] = relationship(back_populates="scheduled_job")

    __table_args__ = (
        CheckConstraint(
            "(is_recurring = true AND cron_expression IS NOT NULL) OR "
            "(is_recurring = false AND run_at IS NOT NULL)",
            name="ck_scheduled_job_has_trigger",
        ),
    )


# --------------------------------------------------------------------------
# Jobs / Executions / Logs
# --------------------------------------------------------------------------

class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    queue_id: Mapped[int] = mapped_column(ForeignKey("queues.id", ondelete="CASCADE"), index=True)
    scheduled_job_id: Mapped[int | None] = mapped_column(ForeignKey("scheduled_jobs.id", ondelete="SET NULL"), nullable=True, index=True)
    batch_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    job_type: Mapped[JobType] = mapped_column(Enum(JobType, name="job_type", values_callable=_enum_values), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status", values_callable=_enum_values),
        default=JobStatus.QUEUED, nullable=False, index=True,
    )

    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    priority: Mapped[int | None] = mapped_column(Integer, nullable=True)  # overrides queue priority when set
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    # retry overrides (fallback to queue.retry_policy when null)
    max_retries_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retry_strategy_override: Mapped[RetryStrategy | None] = mapped_column(
        Enum(RetryStrategy, name="retry_strategy_override", values_callable=_enum_values), nullable=True
    )

    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    claimed_by: Mapped[int | None] = mapped_column(ForeignKey("workers.id", ondelete="SET NULL"), nullable=True, index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    queue: Mapped["Queue"] = relationship(back_populates="jobs")
    scheduled_job: Mapped["ScheduledJob | None"] = relationship(back_populates="jobs")
    worker: Mapped["Worker | None"] = relationship(back_populates="claimed_jobs")
    executions: Mapped[list["JobExecution"]] = relationship(back_populates="job", cascade="all, delete-orphan", order_by="JobExecution.attempt_number")
    logs: Mapped[list["JobLog"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    dlq_entry: Mapped["DeadLetterEntry | None"] = relationship(back_populates="job", uselist=False, cascade="all, delete-orphan")

    __table_args__ = (
        # Speeds up the worker's claim query: WHERE queue_id=? AND status IN (queued, scheduled) AND run_at <= now()
        # ORDER BY priority DESC, run_at ASC
        Index("ix_jobs_claim_lookup", "queue_id", "status", "run_at"),
        Index("ix_jobs_idempotency", "queue_id", "idempotency_key"),
    )


class JobExecution(Base):
    """One row per execution attempt of a job (attempt_number = 1, 2, 3, ...)."""
    __tablename__ = "job_executions"
    __table_args__ = (UniqueConstraint("job_id", "attempt_number", name="uq_job_attempt"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[int | None] = mapped_column(ForeignKey("workers.id", ondelete="SET NULL"), nullable=True, index=True)
    status: Mapped[ExecutionStatus] = mapped_column(
        Enum(ExecutionStatus, name="execution_status", values_callable=_enum_values), default=ExecutionStatus.RUNNING
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    job: Mapped["Job"] = relationship(back_populates="executions")
    worker: Mapped["Worker | None"] = relationship(back_populates="executions")
    logs: Mapped[list["JobLog"]] = relationship(back_populates="execution", cascade="all, delete-orphan")


class JobLog(Base):
    __tablename__ = "job_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    execution_id: Mapped[int | None] = mapped_column(ForeignKey("job_executions.id", ondelete="CASCADE"), nullable=True, index=True)
    level: Mapped[LogLevel] = mapped_column(
        Enum(LogLevel, name="log_level", values_callable=_enum_values), default=LogLevel.INFO
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    job: Mapped["Job"] = relationship(back_populates="logs")
    execution: Mapped["JobExecution | None"] = relationship(back_populates="logs")


class DeadLetterEntry(Base):
    __tablename__ = "dead_letter_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), unique=True, index=True)
    queue_id: Mapped[int] = mapped_column(ForeignKey("queues.id", ondelete="CASCADE"), index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Generated lazily (on first dashboard view of a dead-lettered job, not
    # automatically for every failure) and cached here so repeat views don't
    # re-call the AI summary service. See app/services/ai_summary.py.
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    job: Mapped["Job"] = relationship(back_populates="dlq_entry")


# --------------------------------------------------------------------------
# Workers / Heartbeats
# --------------------------------------------------------------------------

class Worker(Base):
    __tablename__ = "workers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    pid: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[WorkerStatus] = mapped_column(
        Enum(WorkerStatus, name="worker_status", values_callable=_enum_values), default=WorkerStatus.ONLINE, index=True
    )
    concurrency: Mapped[int] = mapped_column(Integer, default=4, nullable=False)
    tags: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    claimed_jobs: Mapped[list["Job"]] = relationship(back_populates="worker")
    executions: Mapped[list["JobExecution"]] = relationship(back_populates="worker")
    heartbeats: Mapped[list["WorkerHeartbeat"]] = relationship(back_populates="worker", cascade="all, delete-orphan")


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    worker_id: Mapped[int] = mapped_column(ForeignKey("workers.id", ondelete="CASCADE"), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    active_job_count: Mapped[int] = mapped_column(Integer, default=0)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)

    worker: Mapped["Worker"] = relationship(back_populates="heartbeats")
