"""Tests for job DAG dependencies (BLOCKED status, promotion on parent
completion, skip cascade on terminal parent failure) and per-execution
timeouts. The DB-backed cases need real Postgres like the rest of the
lifecycle tests; the timeout wrapper is pure asyncio.
"""
import asyncio

from sqlalchemy import select

from app.models import JobLog, JobStatus, JobType, Worker, WorkerStatus
from app.services.job_service import (
    claim_jobs,
    complete_execution,
    create_job,
    fail_execution,
    start_execution,
)
from tests.conftest import requires_db
from tests.factories import make_queue
from worker.runner import run_handler_with_timeout

import pytest


async def _make_worker(db):
    worker = Worker(name="w1", hostname="localhost", pid=1, status=WorkerStatus.ONLINE, concurrency=4)
    db.add(worker)
    await db.flush()
    return worker


async def _run_to_completion(db, worker, job):
    await claim_jobs(db, worker.id, [job.queue_id], limit=10)
    execution = await start_execution(db, job, worker.id)
    await complete_execution(db, job, execution, result={})


@requires_db
async def test_dependent_is_blocked_and_not_claimable(db_session):
    queue = await make_queue(db_session)
    worker = await _make_worker(db_session)
    parent = await create_job(db_session, queue, name="parent", job_type=JobType.IMMEDIATE, payload={})
    child = await create_job(
        db_session, queue, name="child", job_type=JobType.IMMEDIATE, payload={}, depends_on=[parent.id]
    )
    assert child.status == JobStatus.BLOCKED

    claimed = await claim_jobs(db_session, worker.id, [queue.id], limit=10)
    assert [j.id for j in claimed] == [parent.id]  # blocked child never claimed


@requires_db
async def test_dependent_promoted_when_all_parents_complete(db_session):
    queue = await make_queue(db_session)
    worker = await _make_worker(db_session)
    parent_a = await create_job(db_session, queue, name="a", job_type=JobType.IMMEDIATE, payload={})
    parent_b = await create_job(db_session, queue, name="b", job_type=JobType.IMMEDIATE, payload={})
    child = await create_job(
        db_session, queue, name="c", job_type=JobType.IMMEDIATE, payload={}, depends_on=[parent_a.id, parent_b.id]
    )

    await _run_to_completion(db_session, worker, parent_a)
    assert child.status == JobStatus.BLOCKED  # one parent still pending

    await _run_to_completion(db_session, worker, parent_b)
    assert child.status == JobStatus.QUEUED  # both done -> promoted

    claimed = await claim_jobs(db_session, worker.id, [queue.id], limit=10)
    assert child.id in [j.id for j in claimed]


@requires_db
async def test_subtree_skipped_when_parent_dead_letters(db_session):
    queue = await make_queue(db_session, max_retries=0)  # first failure dead-letters
    worker = await _make_worker(db_session)
    parent = await create_job(db_session, queue, name="fetch", job_type=JobType.IMMEDIATE, payload={})
    child = await create_job(db_session, queue, name="diff", job_type=JobType.IMMEDIATE, payload={}, depends_on=[parent.id])
    grandchild = await create_job(db_session, queue, name="notify", job_type=JobType.IMMEDIATE, payload={}, depends_on=[child.id])

    await claim_jobs(db_session, worker.id, [queue.id], limit=10)
    execution = await start_execution(db_session, parent, worker.id)
    await fail_execution(db_session, parent, execution, queue.retry_policy, "boom")

    assert parent.status == JobStatus.DEAD_LETTER
    assert child.status == JobStatus.CANCELLED
    assert grandchild.status == JobStatus.CANCELLED

    logs = (await db_session.execute(select(JobLog).where(JobLog.job_id == child.id))).scalars().all()
    assert any("Skipped" in log.message for log in logs)


@requires_db
async def test_create_with_completed_parent_is_immediately_queued(db_session):
    queue = await make_queue(db_session)
    worker = await _make_worker(db_session)
    parent = await create_job(db_session, queue, name="p", job_type=JobType.IMMEDIATE, payload={})
    await _run_to_completion(db_session, worker, parent)

    child = await create_job(db_session, queue, name="c", job_type=JobType.IMMEDIATE, payload={}, depends_on=[parent.id])
    assert child.status == JobStatus.QUEUED


@requires_db
async def test_create_with_dead_parent_is_skipped(db_session):
    queue = await make_queue(db_session, max_retries=0)
    worker = await _make_worker(db_session)
    parent = await create_job(db_session, queue, name="p", job_type=JobType.IMMEDIATE, payload={})
    await claim_jobs(db_session, worker.id, [queue.id], limit=10)
    execution = await start_execution(db_session, parent, worker.id)
    await fail_execution(db_session, parent, execution, queue.retry_policy, "boom")

    child = await create_job(db_session, queue, name="c", job_type=JobType.IMMEDIATE, payload={}, depends_on=[parent.id])
    assert child.status == JobStatus.CANCELLED


@requires_db
async def test_depends_on_unknown_job_rejected(db_session):
    queue = await make_queue(db_session)
    with pytest.raises(ValueError):
        await create_job(db_session, queue, name="c", job_type=JobType.IMMEDIATE, payload={}, depends_on=[999999])


# ------------------------------------------------------------------
# Per-execution timeouts (pure asyncio, no DB)
# ------------------------------------------------------------------

async def test_timeout_kills_slow_handler():
    async def slow_handler(payload):
        await asyncio.sleep(5)
        return {}

    with pytest.raises(RuntimeError, match="timed out after 0.05s"):
        await run_handler_with_timeout(slow_handler, {}, 0.05)


async def test_no_timeout_passes_result_through():
    async def quick_handler(payload):
        return {"ok": True, "echo": payload["x"]}

    result = await run_handler_with_timeout(quick_handler, {"x": 1}, None)
    assert result == {"ok": True, "echo": 1}

    result = await run_handler_with_timeout(quick_handler, {"x": 2}, 5.0)
    assert result == {"ok": True, "echo": 2}
