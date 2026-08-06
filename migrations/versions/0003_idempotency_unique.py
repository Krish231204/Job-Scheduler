"""enforce idempotency_key uniqueness with a partial unique index

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-07

`create_job()` implemented idempotency as SELECT-then-INSERT with only a
*non-unique* index behind it, so two concurrent requests carrying the same
idempotency_key could both find nothing and both insert -- exactly the
duplicate the key exists to prevent.

The index is partial rather than a plain UniqueConstraint on
(queue_id, idempotency_key) because that would change behavior: the
existing lookup deliberately ignores CANCELLED jobs, so re-submitting a key
whose previous job was cancelled is allowed and should stay allowed. The
predicate below preserves exactly that semantic.

(`idempotency_key IS NOT NULL` is redundant for correctness -- NULLs never
conflict in a Postgres unique index -- but keeps the index off the many
rows that don't use idempotency at all.)
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The old non-unique index served the same (queue_id, idempotency_key)
    # lookup; the partial unique index below replaces it rather than
    # duplicating the write cost of maintaining both.
    op.drop_index("ix_jobs_idempotency", table_name="jobs")
    op.execute(
        """
        CREATE UNIQUE INDEX ix_jobs_idempotency_unique
        ON jobs (queue_id, idempotency_key)
        WHERE idempotency_key IS NOT NULL AND status <> 'cancelled'::job_status
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_jobs_idempotency_unique")
    op.create_index("ix_jobs_idempotency", "jobs", ["queue_id", "idempotency_key"])
