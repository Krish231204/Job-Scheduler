"""watcher application tables (watches, watch_checks, watch_alerts)

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-22

The watcher app is a consumer of the scheduler, not a parallel system:
each due watch materializes a fetch -> diff -> notify job DAG into the
owning organization's watch queue. These tables hold the user-facing
definitions (watches), the check history the dashboard charts
(watch_checks), and transition-based alerts whose unique dedupe_key makes
alerting idempotent (watch_alerts).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# create_type=False: the types are created explicitly at the top of
# upgrade(); without it, create_table would try to CREATE TYPE again.
watch_kind = postgresql.ENUM("down", "keyword", "content_change", name="watch_kind", create_type=False)
watch_state = postgresql.ENUM("unknown", "ok", "triggered", "broken", name="watch_state", create_type=False)
check_outcome = postgresql.ENUM("ok", "triggered", "error", name="check_outcome", create_type=False)
watch_alert_kind = postgresql.ENUM("triggered", "recovered", "broken", name="watch_alert_kind", create_type=False)


def upgrade() -> None:
    watch_kind.create(op.get_bind())
    watch_state.create(op.get_bind())
    check_outcome.create(op.get_bind())
    watch_alert_kind.create(op.get_bind())

    op.create_table(
        "watches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("queue_id", sa.Integer(), sa.ForeignKey("queues.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("url", sa.String(2000), nullable=False),
        sa.Column("kind", watch_kind, nullable=False),
        sa.Column("keyword", sa.String(255), nullable=True),
        sa.Column("interval_seconds", sa.Integer(), nullable=False, server_default="300"),
        sa.Column("webhook_url", sa.String(2000), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("state", watch_state, nullable=False, server_default="unknown"),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_fetch_job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("last_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("interval_seconds >= 60", name="ck_watch_interval_min"),
    )
    op.create_index("ix_watches_organization_id", "watches", ["organization_id"])
    op.create_index("ix_watches_queue_id", "watches", ["queue_id"])
    op.create_index("ix_watches_next_check_at", "watches", ["next_check_at"])

    op.create_table(
        "watch_checks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("watch_id", sa.Integer(), sa.ForeignKey("watches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("fetch_job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("outcome", check_outcome, nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
    )
    op.create_index("ix_watch_checks_watch_id", "watch_checks", ["watch_id"])
    op.create_index("ix_watch_checks_history", "watch_checks", ["watch_id", "started_at"])

    op.create_table(
        "watch_alerts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("watch_id", sa.Integer(), sa.ForeignKey("watches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("check_id", sa.Integer(), sa.ForeignKey("watch_checks.id", ondelete="SET NULL"), nullable=True),
        sa.Column("kind", watch_alert_kind, nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("dedupe_key", sa.String(255), nullable=False, unique=True),
        sa.Column("delivered", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("delivery_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_watch_alerts_watch_id", "watch_alerts", ["watch_id"])
    op.create_index("ix_watch_alerts_created_at", "watch_alerts", ["created_at"])


def downgrade() -> None:
    op.drop_table("watch_alerts")
    op.drop_table("watch_checks")
    op.drop_table("watches")
    watch_alert_kind.drop(op.get_bind())
    check_outcome.drop(op.get_bind())
    watch_state.drop(op.get_bind())
    watch_kind.drop(op.get_bind())
