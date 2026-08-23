"""job DAG dependencies + per-execution timeouts

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-22

Two core execution-model upgrades:

- `job_dependencies` edges + a new `blocked` job status. A job with
  unmet dependencies is created BLOCKED, which the worker's claim query
  never selects (it claims only queued/scheduled), so no claim-path
  change is needed. Promotion/skip logic lives in
  app/services/job_service.py.
- `jobs.timeout_seconds`: per-execution wall-clock limit enforced by the
  worker (asyncio.wait_for); a timed-out attempt fails into the normal
  retry/dead-letter path.

ALTER TYPE ... ADD VALUE is safe inside the migration's transaction on
PostgreSQL 12+ as long as the new value isn't used in the same
transaction -- and it isn't: rows only get status 'blocked' at runtime.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'blocked' BEFORE 'claimed'")

    op.add_column("jobs", sa.Column("timeout_seconds", sa.Float(), nullable=True))

    op.create_table(
        "job_dependencies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("depends_on_job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.UniqueConstraint("job_id", "depends_on_job_id", name="uq_job_dependency"),
    )
    op.create_index("ix_job_dependencies_job_id", "job_dependencies", ["job_id"])
    op.create_index("ix_job_dependencies_depends_on_job_id", "job_dependencies", ["depends_on_job_id"])


def downgrade() -> None:
    op.drop_table("job_dependencies")
    op.drop_column("jobs", "timeout_seconds")
    # Postgres cannot remove a value from an enum type; 'blocked' stays.
