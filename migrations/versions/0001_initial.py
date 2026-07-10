"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-07-10
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(255), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "organizations",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    org_role = sa.Enum("owner", "admin", "member", name="org_role")
    op.create_table(
        "organization_members",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", org_role, nullable=False, server_default="member"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("organization_id", "user_id", name="uq_org_member"),
    )
    op.create_index("ix_org_members_org", "organization_members", ["organization_id"])
    op.create_index("ix_org_members_user", "organization_members", ["user_id"])

    op.create_table(
        "projects",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("organization_id", "name", name="uq_project_name_per_org"),
    )
    op.create_index("ix_projects_org", "projects", ["organization_id"])

    op.create_table(
        "queues",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("priority", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_concurrency", sa.Integer, nullable=False, server_default="4"),
        sa.Column("is_paused", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("project_id", "name", name="uq_queue_name_per_project"),
    )
    op.create_index("ix_queues_project", "queues", ["project_id"])

    retry_strategy = sa.Enum("fixed", "linear", "exponential", name="retry_strategy")
    retry_strategy_override = sa.Enum("fixed", "linear", "exponential", name="retry_strategy_override")
    op.create_table(
        "retry_policies",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("queue_id", sa.Integer, sa.ForeignKey("queues.id", ondelete="CASCADE"), nullable=False),
        sa.Column("strategy", retry_strategy, nullable=False, server_default="exponential"),
        sa.Column("max_retries", sa.Integer, nullable=False, server_default="5"),
        sa.Column("base_delay_seconds", sa.Float, nullable=False, server_default="2.0"),
        sa.Column("multiplier", sa.Float, nullable=False, server_default="2.0"),
        sa.Column("max_delay_seconds", sa.Float, nullable=False, server_default="3600.0"),
        sa.CheckConstraint("max_retries >= 0", name="ck_retry_max_retries_nonneg"),
        sa.CheckConstraint("base_delay_seconds >= 0", name="ck_retry_base_delay_nonneg"),
    )
    op.create_index("ix_retry_policies_queue", "retry_policies", ["queue_id"], unique=True)

    op.create_table(
        "scheduled_jobs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("queue_id", sa.Integer, sa.ForeignKey("queues.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("job_name", sa.String(255), nullable=False),
        sa.Column("payload_template", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("cron_expression", sa.String(120), nullable=True),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_recurring", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "(is_recurring = true AND cron_expression IS NOT NULL) OR "
            "(is_recurring = false AND run_at IS NOT NULL)",
            name="ck_scheduled_job_has_trigger",
        ),
    )
    op.create_index("ix_scheduled_jobs_queue", "scheduled_jobs", ["queue_id"])
    op.create_index("ix_scheduled_jobs_next_run", "scheduled_jobs", ["next_run_at"])

    worker_status = sa.Enum("online", "draining", "offline", name="worker_status")
    op.create_table(
        "workers",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("hostname", sa.String(255), nullable=False),
        sa.Column("pid", sa.Integer, nullable=False),
        sa.Column("status", worker_status, nullable=False, server_default="online"),
        sa.Column("concurrency", sa.Integer, nullable=False, server_default="4"),
        sa.Column("tags", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_workers_status", "workers", ["status"])
    op.create_index("ix_workers_last_seen", "workers", ["last_seen_at"])

    job_type = sa.Enum("immediate", "delayed", "scheduled", "recurring", "batch", name="job_type")
    job_status = sa.Enum(
        "queued", "scheduled", "claimed", "running", "completed", "failed",
        "retrying", "dead_letter", "cancelled", name="job_status",
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("queue_id", sa.Integer, sa.ForeignKey("queues.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scheduled_job_id", sa.Integer, sa.ForeignKey("scheduled_jobs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("batch_id", sa.String(64), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("job_type", job_type, nullable=False),
        sa.Column("status", job_status, nullable=False, server_default="queued"),
        sa.Column("payload", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("priority", sa.Integer, nullable=True),
        sa.Column("idempotency_key", sa.String(255), nullable=True),
        sa.Column("run_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("max_retries_override", sa.Integer, nullable=True),
        sa.Column("retry_strategy_override", retry_strategy_override, nullable=True),
        sa.Column("attempt_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.Integer, sa.ForeignKey("workers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_jobs_queue", "jobs", ["queue_id"])
    op.create_index("ix_jobs_scheduled_job", "jobs", ["scheduled_job_id"])
    op.create_index("ix_jobs_batch", "jobs", ["batch_id"])
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_index("ix_jobs_idempotency_key", "jobs", ["idempotency_key"])
    op.create_index("ix_jobs_run_at", "jobs", ["run_at"])
    op.create_index("ix_jobs_claimed_by", "jobs", ["claimed_by"])
    # Primary index the worker's claim query relies on (see job_service.claim_jobs).
    op.create_index("ix_jobs_claim_lookup", "jobs", ["queue_id", "status", "run_at"])
    op.create_index("ix_jobs_idempotency", "jobs", ["queue_id", "idempotency_key"])

    execution_status = sa.Enum("running", "succeeded", "failed", name="execution_status")
    op.create_table(
        "job_executions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("job_id", sa.Integer, sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("attempt_number", sa.Integer, nullable=False),
        sa.Column("worker_id", sa.Integer, sa.ForeignKey("workers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", execution_status, nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("result", sa.JSON, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.UniqueConstraint("job_id", "attempt_number", name="uq_job_attempt"),
    )
    op.create_index("ix_job_executions_job", "job_executions", ["job_id"])
    op.create_index("ix_job_executions_worker", "job_executions", ["worker_id"])

    log_level = sa.Enum("debug", "info", "warning", "error", name="log_level")
    op.create_table(
        "job_logs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("job_id", sa.Integer, sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("execution_id", sa.Integer, sa.ForeignKey("job_executions.id", ondelete="CASCADE"), nullable=True),
        sa.Column("level", log_level, nullable=False, server_default="info"),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_job_logs_job", "job_logs", ["job_id"])
    op.create_index("ix_job_logs_execution", "job_logs", ["execution_id"])
    op.create_index("ix_job_logs_timestamp", "job_logs", ["timestamp"])

    op.create_table(
        "dead_letter_entries",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("job_id", sa.Integer, sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("queue_id", sa.Integer, sa.ForeignKey("queues.id", ondelete="CASCADE"), nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("attempt_count", sa.Integer, nullable=False),
        sa.Column("payload_snapshot", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("failed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_dlq_job", "dead_letter_entries", ["job_id"], unique=True)
    op.create_index("ix_dlq_queue", "dead_letter_entries", ["queue_id"])

    op.create_table(
        "worker_heartbeats",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("worker_id", sa.Integer, sa.ForeignKey("workers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("active_job_count", sa.Integer, server_default="0"),
        sa.Column("metrics", sa.JSON, nullable=False, server_default="{}"),
    )
    op.create_index("ix_worker_heartbeats_worker", "worker_heartbeats", ["worker_id"])
    op.create_index("ix_worker_heartbeats_timestamp", "worker_heartbeats", ["timestamp"])


def downgrade() -> None:
    op.drop_table("worker_heartbeats")
    op.drop_table("dead_letter_entries")
    op.drop_table("job_logs")
    op.drop_table("job_executions")
    op.drop_table("jobs")
    op.drop_table("workers")
    op.drop_table("scheduled_jobs")
    op.drop_table("retry_policies")
    op.drop_table("queues")
    op.drop_table("projects")
    op.drop_table("organization_members")
    op.drop_table("organizations")
    op.drop_table("users")

    sa.Enum(name="log_level").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="execution_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="job_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="job_type").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="worker_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="retry_strategy_override").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="retry_strategy").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="org_role").drop(op.get_bind(), checkfirst=True)
