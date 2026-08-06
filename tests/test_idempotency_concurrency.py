"""Concurrent submission with the same idempotency_key must create one job.

Companion to test_claim_concurrency.py: that one proves no job is claimed
twice, this one proves no job is *created* twice. Both need real Postgres
with genuinely concurrent, independently-committed transactions -- the
failure mode here (two callers both SELECT nothing, both INSERT) is
invisible to a single-session test, which is exactly how it survived in the
codebase behind a non-unique index.
"""
import asyncio

import app.services.job_service as job_service
from sqlalchemy import func, select

from app.models import Job, JobStatus, JobType, Queue
from app.services.job_service import create_job
from tests.conftest import requires_db
from tests.factories import make_queue

CONCURRENCY = 8


@requires_db
async def test_concurrent_submissions_with_same_key_create_one_job(session_factory, monkeypatch):
    """Forces the exact interleaving the partial unique index exists to stop:
    every caller completes its "does this key already exist?" lookup *before*
    any of them inserts.

    The barrier is load-bearing, not decoration. Plain `asyncio.gather` was
    tried first and the tasks serialized -- each finished its insert before
    the next one looked up, so the test passed even with the unique index
    removed, i.e. it proved nothing. With the barrier it fails (8 duplicate
    rows) against a non-unique index and passes against the real one.
    """
    async with session_factory() as setup:
        queue = await make_queue(setup)
        queue_id = queue.id
        await setup.commit()

    key = "welcome-email-user-42"
    barrier = asyncio.Barrier(CONCURRENCY)
    barriered_tasks: set = set()
    original_lookup = job_service._find_live_job_by_key

    async def lookup_then_wait(db, q_id, k):
        result = await original_lookup(db, q_id, k)
        # Only the pre-insert lookup waits. The recovery-path lookup (after
        # IntegrityError) must not, or the losers would deadlock waiting for
        # parties that have already gone through.
        task = asyncio.current_task()
        if task not in barriered_tasks:
            barriered_tasks.add(task)
            await barrier.wait()
        return result

    monkeypatch.setattr(job_service, "_find_live_job_by_key", lookup_then_wait)

    async def submit() -> int:
        async with session_factory() as db:
            queue = await db.get(Queue, queue_id)
            job = await create_job(
                db, queue, name="send-email", job_type=JobType.IMMEDIATE,
                payload={"to": "a@example.com"}, idempotency_key=key,
            )
            job_id = job.id
            await db.commit()
            return job_id

    job_ids = await asyncio.gather(*(submit() for _ in range(CONCURRENCY)))

    # Every caller must get the same job back -- the losers of the insert
    # race re-read the winner's row rather than erroring or duplicating.
    assert len(set(job_ids)) == 1, f"expected one job, callers got {sorted(set(job_ids))}"

    async with session_factory() as db:
        count = (
            await db.execute(
                select(func.count(Job.id)).where(Job.queue_id == queue_id, Job.idempotency_key == key)
            )
        ).scalar_one()
    assert count == 1, f"{count} rows written for a single idempotency key"


@requires_db
async def test_key_is_reusable_after_the_previous_job_is_cancelled(session_factory):
    """The unique index is partial (excludes CANCELLED) on purpose -- this
    locks in that the fix didn't quietly turn idempotency keys into
    permanently-burned single-use tokens.
    """
    key = "nightly-report"

    async with session_factory() as db:
        queue = await make_queue(db)
        queue_id = queue.id
        first = await create_job(
            db, queue, name="report", job_type=JobType.IMMEDIATE, payload={}, idempotency_key=key
        )
        first_id = first.id
        await db.commit()

    async with session_factory() as db:
        job = await db.get(Job, first_id)
        job.status = JobStatus.CANCELLED
        await db.commit()

    async with session_factory() as db:
        queue = await db.get(Queue, queue_id)
        second = await create_job(
            db, queue, name="report", job_type=JobType.IMMEDIATE, payload={}, idempotency_key=key
        )
        await db.commit()
        assert second.id != first_id, "cancelled job should not block reuse of its idempotency key"
