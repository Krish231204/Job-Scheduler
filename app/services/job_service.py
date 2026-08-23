"""Core job lifecycle operations shared by the API and the worker/scheduler
processes. Anything that touches Job.status transitions lives here so the
state machine has one implementation.

Job status state machine:

    QUEUED/SCHEDULED --(worker claims)--> CLAIMED --(worker starts)--> RUNNING
        RUNNING --(success)--> COMPLETED
        RUNNING --(failure, retries left)--> RETRYING --(delay elapses)--> QUEUED
        RUNNING --(failure, retries exhausted)--> DEAD_LETTER
"""
import uuid
from datetime import datetime, timedelta, timezone

from croniter import croniter
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    DeadLetterEntry,
    Job,
    JobDependency,
    JobExecution,
    JobLog,
    JobStatus,
    JobType,
    LogLevel,
    Queue,
    RetryPolicy,
    RetryStrategy,
    ScheduledJob,
)
from app.services.retry import compute_retry_delay_seconds, should_dead_letter


def _effective_retry_params(job: Job, retry_policy: RetryPolicy | None) -> tuple[RetryStrategy, int, float, float, float]:
    strategy = job.retry_strategy_override or (retry_policy.strategy if retry_policy else RetryStrategy.EXPONENTIAL)
    max_retries = job.max_retries_override if job.max_retries_override is not None else (retry_policy.max_retries if retry_policy else 5)
    base_delay = retry_policy.base_delay_seconds if retry_policy else 2.0
    multiplier = retry_policy.multiplier if retry_policy else 2.0
    max_delay = retry_policy.max_delay_seconds if retry_policy else 3600.0
    return strategy, max_retries, base_delay, multiplier, max_delay


async def create_job(
    db: AsyncSession,
    queue: Queue,
    *,
    name: str,
    job_type: JobType,
    payload: dict,
    priority: int | None = None,
    idempotency_key: str | None = None,
    delay_seconds: float | None = None,
    run_at: datetime | None = None,
    max_retries: int | None = None,
    retry_strategy: RetryStrategy | None = None,
    batch_id: str | None = None,
    scheduled_job_id: int | None = None,
    depends_on: list[int] | None = None,
    timeout_seconds: float | None = None,
) -> Job:
    """Create a single job row. Idempotent when idempotency_key is supplied
    and already present (unresolved) on the queue: returns the existing job
    instead of creating a duplicate.

    `depends_on` makes this job a DAG node: it is created BLOCKED until
    every listed parent job COMPLETEs (promotion happens in
    complete_execution via _promote_dependents). If a parent has already
    failed terminally, the job is created CANCELLED immediately -- same
    outcome the skip cascade would produce, just without the detour.

    The guarantee is enforced by a partial unique index (migration 0003),
    not by the SELECT below -- the SELECT is only a fast path that avoids
    raising in the common, uncontended case. Two concurrent callers with
    the same key will both find nothing here and both attempt the INSERT;
    the database rejects the loser, which then re-reads and returns the
    winner's row. See `_find_live_job_by_key`.
    """
    if idempotency_key:
        found = await _find_live_job_by_key(db, queue.id, idempotency_key)
        if found is not None:
            return found

    now = datetime.now(timezone.utc)
    if job_type == JobType.IMMEDIATE:
        effective_run_at = now
        status = JobStatus.QUEUED
    elif job_type == JobType.DELAYED:
        effective_run_at = now + timedelta(seconds=delay_seconds or 0)
        status = JobStatus.SCHEDULED
    elif job_type in (JobType.SCHEDULED, JobType.RECURRING):
        effective_run_at = run_at or now
        status = JobStatus.SCHEDULED
    elif job_type == JobType.BATCH:
        effective_run_at = now
        status = JobStatus.QUEUED
    else:
        raise ValueError(f"Unsupported job_type {job_type}")

    skip_reason: str | None = None
    if depends_on:
        parents_result = await db.execute(
            select(Job).where(Job.id.in_(depends_on), Job.queue_id == queue.id)
        )
        parents = list(parents_result.scalars().all())
        if len(parents) != len(set(depends_on)):
            raise ValueError("depends_on references jobs that don't exist on this queue")
        terminal = [p for p in parents if p.status in (JobStatus.DEAD_LETTER, JobStatus.CANCELLED)]
        if terminal:
            status = JobStatus.CANCELLED
            skip_reason = f"Skipped: upstream job #{terminal[0].id} is {terminal[0].status.value}"
        elif not all(p.status == JobStatus.COMPLETED for p in parents):
            status = JobStatus.BLOCKED

    job = Job(
        queue_id=queue.id,
        scheduled_job_id=scheduled_job_id,
        batch_id=batch_id,
        name=name,
        job_type=job_type,
        status=status,
        payload=payload,
        priority=priority,
        idempotency_key=idempotency_key,
        run_at=effective_run_at,
        max_retries_override=max_retries,
        retry_strategy_override=retry_strategy,
        timeout_seconds=timeout_seconds,
    )
    if not idempotency_key:
        db.add(job)
        await db.flush()
        await _record_dependencies(db, job, depends_on, skip_reason)
        return job

    # Both the add and the flush go inside the SAVEPOINT so that losing the
    # insert race rolls back just this statement. Adding the instance
    # *before* opening the savepoint doesn't work: the failed flush then
    # poisons the caller's whole transaction (PendingRollbackError) and the
    # re-read below can't run.
    try:
        async with db.begin_nested():
            db.add(job)
            await db.flush()
    except IntegrityError:
        # No need to expunge `job` -- rolling back the SAVEPOINT already
        # evicted the rejected pending instance from the session.
        #
        # Postgres blocks the duplicate INSERT until the other transaction
        # commits, so by the time we're here the winner's row is committed
        # and visible to this (READ COMMITTED) statement.
        found = await _find_live_job_by_key(db, queue.id, idempotency_key)
        if found is not None:
            return found
        raise  # unique violation from something other than the idempotency race
    await _record_dependencies(db, job, depends_on, skip_reason)
    return job


async def _record_dependencies(db: AsyncSession, job: Job, depends_on: list[int] | None, skip_reason: str | None) -> None:
    if not depends_on:
        return
    for parent_id in set(depends_on):
        db.add(JobDependency(job_id=job.id, depends_on_job_id=parent_id))
    if skip_reason:
        db.add(JobLog(job_id=job.id, level=LogLevel.WARNING, message=skip_reason))
    await db.flush()


async def _find_live_job_by_key(db: AsyncSession, queue_id: int, idempotency_key: str) -> Job | None:
    """The lookup backing idempotent creation: an existing job on this queue
    with this key that hasn't been cancelled. Kept in one place because it
    has to stay in exact lockstep with the partial unique index's predicate
    in migration 0003 -- if these two ever disagree, idempotency silently
    breaks in one direction or the other.
    """
    result = await db.execute(
        select(Job).where(
            Job.queue_id == queue_id,
            Job.idempotency_key == idempotency_key,
            Job.status.notin_([JobStatus.CANCELLED]),
        )
    )
    return result.scalar_one_or_none()


async def create_batch(
    db: AsyncSession,
    queue: Queue,
    *,
    name: str,
    items: list[dict],
    priority: int | None = None,
    max_retries: int | None = None,
    retry_strategy: RetryStrategy | None = None,
) -> list[Job]:
    batch_id = uuid.uuid4().hex
    jobs = []
    for i, item in enumerate(items):
        job = await create_job(
            db,
            queue,
            name=f"{name}[{i}]",
            job_type=JobType.BATCH,
            payload=item,
            priority=priority,
            max_retries=max_retries,
            retry_strategy=retry_strategy,
            batch_id=batch_id,
        )
        jobs.append(job)
    return jobs


async def claim_jobs(db: AsyncSession, worker_id: int, queue_ids: list[int], limit: int) -> list[Job]:
    """Atomically claim up to `limit` eligible jobs across the given queues.

    Uses SELECT ... FOR UPDATE SKIP LOCKED so that N workers polling
    concurrently never claim the same row twice and never block on rows
    another worker already has locked (Postgres row-level locking).
    Ordering is by queue priority (desc) then run_at (asc) i.e. oldest
    eligible job in the highest priority queue first.
    """
    if not queue_ids:
        return []

    now = datetime.now(timezone.utc)
    stmt = (
        select(Job)
        .join(Queue, Queue.id == Job.queue_id)
        .where(
            Job.queue_id.in_(queue_ids),
            Job.status.in_([JobStatus.QUEUED, JobStatus.SCHEDULED]),
            Job.run_at <= now,
            Queue.is_paused.is_(False),
        )
        .order_by(Queue.priority.desc(), Job.priority.desc().nullslast(), Job.run_at.asc())
        .limit(limit)
        .with_for_update(of=Job, skip_locked=True)
    )
    result = await db.execute(stmt)
    jobs = list(result.scalars().all())

    for job in jobs:
        job.status = JobStatus.CLAIMED
        job.claimed_by = worker_id
        job.claimed_at = now

    await db.flush()
    return jobs


async def start_execution(db: AsyncSession, job: Job, worker_id: int) -> JobExecution:
    job.status = JobStatus.RUNNING
    job.started_at = datetime.now(timezone.utc)
    job.attempt_count += 1

    execution = JobExecution(
        job_id=job.id,
        attempt_number=job.attempt_count,
        worker_id=worker_id,
        status="running",
        # Set explicitly (tz-aware) rather than leaning on the column's
        # server_default: a server-generated value isn't populated on the
        # instance until it's refreshed, and what came back could be naive,
        # which is why the duration math below used to patch it with
        # .replace(tzinfo=utc). Setting it here makes that patch unnecessary
        # and keeps this consistent with job.started_at above.
        started_at=datetime.now(timezone.utc),
    )
    db.add(execution)
    await db.flush()
    return execution


async def complete_execution(db: AsyncSession, job: Job, execution: JobExecution, result: dict | None) -> None:
    now = datetime.now(timezone.utc)
    execution.status = "succeeded"
    execution.finished_at = now
    execution.result = result
    execution.duration_ms = int((now - execution.started_at).total_seconds() * 1000) if execution.started_at else None

    job.status = JobStatus.COMPLETED
    job.completed_at = now
    db.add(JobLog(job_id=job.id, execution_id=execution.id, level=LogLevel.INFO, message="Job completed successfully"))
    await db.flush()
    await _promote_dependents(db, job)


async def _promote_dependents(db: AsyncSession, job: Job) -> int:
    """Unblock jobs that were waiting on `job` once ALL their parents have
    completed. The dependent row is locked with a *waiting* FOR UPDATE
    (not SKIP LOCKED) on purpose: when two parents of the same child
    complete concurrently, each transaction's snapshot may miss the
    other's not-yet-committed COMPLETED status. Serializing on the child
    row means the second completer re-reads parent statuses after the
    first has committed, so exactly one of them performs the promotion
    and none is missed.
    """
    dependent_ids = (
        await db.execute(select(JobDependency.job_id).where(JobDependency.depends_on_job_id == job.id))
    ).scalars().all()
    promoted = 0
    now = datetime.now(timezone.utc)
    for dep_id in dependent_ids:
        dependent = (
            await db.execute(select(Job).where(Job.id == dep_id).with_for_update())
        ).scalar_one_or_none()
        if dependent is None or dependent.status != JobStatus.BLOCKED:
            continue
        unmet = (
            await db.execute(
                select(func.count(JobDependency.id))
                .join(Job, Job.id == JobDependency.depends_on_job_id)
                .where(JobDependency.job_id == dep_id, Job.status != JobStatus.COMPLETED)
            )
        ).scalar_one()
        if unmet == 0:
            dependent.status = JobStatus.QUEUED
            dependent.run_at = now
            db.add(JobLog(job_id=dep_id, level=LogLevel.INFO, message="All dependencies completed; job queued"))
            promoted += 1
    await db.flush()
    return promoted


async def _skip_dependents(db: AsyncSession, job: Job, reason: str) -> int:
    """Cancel the entire BLOCKED subtree downstream of a terminally-failed
    job (dead-lettered or cancelled). Leaving them BLOCKED would strand
    them forever -- their parent can never COMPLETE. Iterative BFS so a
    fetch -> diff -> notify chain (or deeper) skips end to end.
    """
    skipped = 0
    frontier = [job.id]
    while frontier:
        dependent_ids = (
            await db.execute(select(JobDependency.job_id).where(JobDependency.depends_on_job_id.in_(frontier)))
        ).scalars().all()
        frontier = []
        for dep_id in dependent_ids:
            dependent = (
                await db.execute(select(Job).where(Job.id == dep_id).with_for_update())
            ).scalar_one_or_none()
            if dependent is None or dependent.status != JobStatus.BLOCKED:
                continue
            dependent.status = JobStatus.CANCELLED
            db.add(JobLog(job_id=dep_id, level=LogLevel.WARNING, message=reason))
            skipped += 1
            frontier.append(dep_id)
    await db.flush()
    return skipped


async def fail_execution(
    db: AsyncSession,
    job: Job,
    execution: JobExecution,
    retry_policy: RetryPolicy | None,
    error: str,
) -> None:
    now = datetime.now(timezone.utc)
    execution.status = "failed"
    execution.finished_at = now
    execution.error = error
    execution.duration_ms = int((now - execution.started_at).total_seconds() * 1000) if execution.started_at else None

    strategy, max_retries, base_delay, multiplier, max_delay = _effective_retry_params(job, retry_policy)

    db.add(JobLog(job_id=job.id, execution_id=execution.id, level=LogLevel.ERROR, message=f"Attempt {job.attempt_count} failed: {error}"))

    if should_dead_letter(job.attempt_count, max_retries):
        job.status = JobStatus.DEAD_LETTER
        job.completed_at = now
        db.add(DeadLetterEntry(
            job_id=job.id,
            queue_id=job.queue_id,
            reason=error,
            attempt_count=job.attempt_count,
            payload_snapshot=job.payload,
        ))
        db.add(JobLog(job_id=job.id, level=LogLevel.ERROR, message="Max retries exhausted; moved to dead letter queue"))
        await _skip_dependents(db, job, f"Skipped: upstream job #{job.id} dead-lettered")
    else:
        delay = compute_retry_delay_seconds(strategy, job.attempt_count, base_delay, multiplier, max_delay)
        job.status = JobStatus.RETRYING
        job.next_retry_at = now + timedelta(seconds=delay)
        db.add(JobLog(job_id=job.id, level=LogLevel.WARNING, message=f"Retry scheduled in {delay:.1f}s (attempt {job.attempt_count + 1})"))

    await db.flush()


async def promote_retrying_jobs(db: AsyncSession) -> int:
    """Move RETRYING jobs whose next_retry_at has elapsed back to QUEUED so
    workers can pick them up again. Called by the scheduler loop.
    """
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(Job).where(Job.status == JobStatus.RETRYING, Job.next_retry_at <= now).with_for_update(skip_locked=True)
    )
    jobs = list(result.scalars().all())
    for job in jobs:
        job.status = JobStatus.QUEUED
        job.run_at = now
        job.claimed_by = None
        job.claimed_at = None
    await db.flush()
    return len(jobs)


async def materialize_due_scheduled_jobs(db: AsyncSession) -> int:
    """For every active ScheduledJob whose next_run_at has elapsed, create a
    concrete Job row and advance next_run_at (cron) or deactivate (one-off).
    """
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(ScheduledJob).where(ScheduledJob.is_active.is_(True), ScheduledJob.next_run_at <= now)
        .with_for_update(skip_locked=True)
    )
    due = list(result.scalars().all())

    for sj in due:
        queue = await db.get(Queue, sj.queue_id)
        job_type = JobType.RECURRING if sj.is_recurring else JobType.SCHEDULED
        await create_job(
            db,
            queue,
            name=sj.job_name,
            job_type=job_type,
            payload=dict(sj.payload_template),
            run_at=now,
            scheduled_job_id=sj.id,
        )
        sj.last_run_at = now
        if sj.is_recurring and sj.cron_expression:
            sj.next_run_at = croniter(sj.cron_expression, now).get_next(datetime)
        else:
            sj.is_active = False

    await db.flush()
    return len(due)


def compute_initial_next_run(cron_expression: str | None, run_at: datetime | None, is_recurring: bool) -> datetime:
    now = datetime.now(timezone.utc)
    if is_recurring and cron_expression:
        return croniter(cron_expression, now).get_next(datetime)
    return run_at or now


async def detect_stale_workers(db: AsyncSession, timeout_seconds: float) -> int:
    """Mark workers as offline and requeue jobs they were running if their
    heartbeat is older than `timeout_seconds` (crash/network partition
    recovery). Returns number of jobs requeued.
    """
    from app.models import Worker, WorkerStatus  # local import to avoid cycle at module load

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)
    result = await db.execute(
        select(Worker).where(Worker.status != WorkerStatus.OFFLINE, Worker.last_seen_at < cutoff)
    )
    stale_workers = list(result.scalars().all())
    requeued = 0
    for worker in stale_workers:
        worker.status = WorkerStatus.OFFLINE
        jobs_result = await db.execute(
            select(Job).where(
                Job.claimed_by == worker.id,
                Job.status.in_([JobStatus.CLAIMED, JobStatus.RUNNING]),
            )
        )
        for job in jobs_result.scalars().all():
            job.status = JobStatus.QUEUED
            job.claimed_by = None
            job.claimed_at = None
            job.run_at = datetime.now(timezone.utc)
            db.add(JobLog(job_id=job.id, level=LogLevel.WARNING, message=f"Requeued: worker {worker.id} missed heartbeat deadline"))
            requeued += 1
    await db.flush()
    return requeued
