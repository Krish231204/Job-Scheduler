
from sqlalchemy import select

from app.models import DeadLetterEntry, JobStatus, JobType, Worker, WorkerStatus
from app.services.job_service import (
    claim_jobs,
    complete_execution,
    create_job,
    fail_execution,
    promote_retrying_jobs,
    start_execution,
)
from tests.conftest import requires_db
from tests.factories import make_queue


async def _make_worker(db):
    worker = Worker(name="w1", hostname="localhost", pid=1, status=WorkerStatus.ONLINE, concurrency=4)
    db.add(worker)
    await db.flush()
    return worker


async def _dlq_entry(db, job_id):
    # Query directly rather than `job.dlq_entry` -- lazy-loading a relationship
    # via plain attribute access on an AsyncSession-bound object outside of an
    # awaited SQLAlchemy call raises `MissingGreenlet`. This isn't a bug in
    # the app (job_service.py never does this); it's specific to how the
    # async ORM's implicit lazy-load trick is only available inside its own
    # greenlet context, not from arbitrary test code.
    result = await db.execute(select(DeadLetterEntry).where(DeadLetterEntry.job_id == job_id))
    return result.scalar_one_or_none()


@requires_db
async def test_immediate_job_is_claimable_right_away(db_session):
    queue = await make_queue(db_session)
    job = await create_job(db_session, queue, name="t", job_type=JobType.IMMEDIATE, payload={})
    assert job.status == JobStatus.QUEUED

    worker = await _make_worker(db_session)
    claimed = await claim_jobs(db_session, worker.id, [queue.id], limit=10)
    assert [j.id for j in claimed] == [job.id]
    assert job.status == JobStatus.CLAIMED
    assert job.claimed_by == worker.id


@requires_db
async def test_delayed_job_not_claimable_before_delay_elapses(db_session):
    queue = await make_queue(db_session)
    job = await create_job(db_session, queue, name="t", job_type=JobType.DELAYED, payload={}, delay_seconds=3600)
    assert job.status == JobStatus.SCHEDULED

    worker = await _make_worker(db_session)
    claimed = await claim_jobs(db_session, worker.id, [queue.id], limit=10)
    assert claimed == []


@requires_db
async def test_successful_execution_marks_job_completed(db_session):
    queue = await make_queue(db_session)
    job = await create_job(db_session, queue, name="t", job_type=JobType.IMMEDIATE, payload={})
    worker = await _make_worker(db_session)
    await claim_jobs(db_session, worker.id, [queue.id], limit=10)

    execution = await start_execution(db_session, job, worker.id)
    assert job.status == JobStatus.RUNNING
    assert job.attempt_count == 1

    await complete_execution(db_session, job, execution, result={"ok": True})
    assert job.status == JobStatus.COMPLETED
    assert job.completed_at is not None


@requires_db
async def test_failed_execution_schedules_retry_then_dead_letters_after_max_retries(db_session):
    # max_retries=2 -> attempts 1 and 2 fail into RETRYING, attempt 3 fails into DEAD_LETTER.
    queue = await make_queue(db_session, max_retries=2, base_delay_seconds=0.0)
    job = await create_job(db_session, queue, name="t", job_type=JobType.IMMEDIATE, payload={})
    worker = await _make_worker(db_session)

    for expected_attempt in (1, 2):
        await claim_jobs(db_session, worker.id, [queue.id], limit=10)
        execution = await start_execution(db_session, job, worker.id)
        assert job.attempt_count == expected_attempt
        await fail_execution(db_session, job, execution, queue.retry_policy, error="boom")
        assert job.status == JobStatus.RETRYING
        assert await _dlq_entry(db_session, job.id) is None

        promoted = await promote_retrying_jobs(db_session)
        assert promoted == 1
        assert job.status == JobStatus.QUEUED

    # Third and final attempt exhausts retries.
    await claim_jobs(db_session, worker.id, [queue.id], limit=10)
    execution = await start_execution(db_session, job, worker.id)
    assert job.attempt_count == 3
    await fail_execution(db_session, job, execution, queue.retry_policy, error="boom again")

    assert job.status == JobStatus.DEAD_LETTER
    dlq_entry = await _dlq_entry(db_session, job.id)
    assert dlq_entry is not None
    assert dlq_entry.attempt_count == 3


@requires_db
async def test_paused_queue_is_never_claimed_from(db_session):
    queue = await make_queue(db_session)
    queue.is_paused = True
    await create_job(db_session, queue, name="t", job_type=JobType.IMMEDIATE, payload={})
    worker = await _make_worker(db_session)

    claimed = await claim_jobs(db_session, worker.id, [queue.id], limit=10)
    assert claimed == []


@requires_db
async def test_queue_max_concurrency_limits_claim_batch_size(db_session):
    queue = await make_queue(db_session, max_concurrency=2)
    for i in range(5):
        await create_job(db_session, queue, name=f"t{i}", job_type=JobType.IMMEDIATE, payload={})
    worker = await _make_worker(db_session)

    # claim_jobs itself doesn't enforce max_concurrency (that's the worker's
    # job -- see WorkerRunner._claimable_capacity); it only enforces the
    # `limit` passed in, which the worker computes from max_concurrency minus
    # in-flight jobs. This test locks in that contract.
    claimed = await claim_jobs(db_session, worker.id, [queue.id], limit=queue.max_concurrency)
    assert len(claimed) == 2
