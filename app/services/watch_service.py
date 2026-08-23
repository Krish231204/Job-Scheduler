"""Watch lifecycle: provisioning, tick materialization, and alerts.

A watch is checked by materializing a three-job DAG into the owning
organization's dedicated watch queue on every due tick:

    watch_fetch  --(depends_on)-->  watch_diff  --(depends_on)-->  watch_notify

- fetch performs the HTTP request (robots/rate-limit/SSRF-guarded, see
  worker/watch_handlers.py) and records a WatchCheck row;
- diff evaluates the watch's condition against that check (and the
  previous one), transitions the watch state, and creates a transition
  alert if warranted;
- notify delivers undelivered alerts (webhook, if configured).

Failure semantics come from the scheduler itself: fetch errors retry per
the watch queue's retry policy and dead-letter when exhausted, which
cancels the tick's diff/notify via the DAG skip cascade. A watch whose
fetch dead-letters BROKEN_AFTER_CONSECUTIVE_FAILURES ticks in a row is
marked broken and deactivated (with an idempotent 'broken' alert) --
that's the dead-lettering-for-permanently-broken-targets behavior.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Job,
    JobStatus,
    JobType,
    Organization,
    Project,
    Queue,
    RetryPolicy,
    RetryStrategy,
    Watch,
    WatchAlert,
    WatchAlertKind,
    WatchState,
)
from app.services.job_service import create_job

logger = logging.getLogger("jobsched.watch")

WATCH_PROJECT_NAME = "Watches"
WATCH_QUEUE_NAME = "watch-checks"
BROKEN_AFTER_CONSECUTIVE_FAILURES = 3
CHECK_TIMEOUT_SECONDS = 30.0
_TERMINAL = (JobStatus.COMPLETED, JobStatus.DEAD_LETTER, JobStatus.CANCELLED)


async def get_or_create_watch_queue(db: AsyncSession, organization_id: int) -> Queue:
    """Each org gets an auto-provisioned Watches project + watch-checks
    queue on first use, so watch traffic shows up in the normal dashboard
    with its own stats instead of polluting user queues."""
    result = await db.execute(
        select(Queue)
        .join(Project, Project.id == Queue.project_id)
        .where(Project.organization_id == organization_id, Project.name == WATCH_PROJECT_NAME, Queue.name == WATCH_QUEUE_NAME)
    )
    queue = result.scalar_one_or_none()
    if queue is not None:
        return queue

    org = await db.get(Organization, organization_id)
    if org is None:
        raise ValueError(f"Organization {organization_id} not found")

    project_result = await db.execute(
        select(Project).where(Project.organization_id == organization_id, Project.name == WATCH_PROJECT_NAME)
    )
    project = project_result.scalar_one_or_none()
    if project is None:
        project = Project(
            organization_id=organization_id,
            name=WATCH_PROJECT_NAME,
            description="Auto-provisioned: jobs that run this organization's watches.",
        )
        db.add(project)
        await db.flush()

    queue = Queue(project_id=project.id, name=WATCH_QUEUE_NAME, priority=0, max_concurrency=8)
    db.add(queue)
    await db.flush()
    db.add(RetryPolicy(
        queue_id=queue.id,
        strategy=RetryStrategy.EXPONENTIAL,
        max_retries=2,
        base_delay_seconds=5.0,
        multiplier=2.0,
        max_delay_seconds=60.0,
    ))
    await db.flush()
    logger.info("Provisioned watch queue for org=%s (queue=%s)", organization_id, queue.id)
    return queue


async def create_alert(
    db: AsyncSession,
    watch: Watch,
    *,
    check_id: int | None,
    kind: WatchAlertKind,
    message: str,
    dedupe_key: str,
) -> WatchAlert | None:
    """Insert an alert idempotently: the unique dedupe_key makes a repeat
    of the same logical alert a no-op instead of a duplicate, whatever
    races or retries produced the repeat."""
    alert = WatchAlert(watch_id=watch.id, check_id=check_id, kind=kind, message=message, dedupe_key=dedupe_key)
    try:
        async with db.begin_nested():
            db.add(alert)
            await db.flush()
    except IntegrityError:
        return None  # already alerted for this transition
    return alert


async def materialize_due_watches(db: AsyncSession) -> int:
    """Create the fetch->diff->notify DAG for every active watch whose
    next_check_at has elapsed. Called from the scheduler loop; SKIP LOCKED
    keeps concurrent scheduler instances from double-materializing."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(Watch)
        .where(Watch.is_active.is_(True), Watch.next_check_at <= now)
        .with_for_update(skip_locked=True)
    )
    due = list(result.scalars().all())
    materialized = 0

    for watch in due:
        last_fetch = await db.get(Job, watch.last_fetch_job_id) if watch.last_fetch_job_id else None

        # Previous tick still in flight (slow target + short interval):
        # push this tick instead of stacking concurrent checks of the
        # same URL.
        if last_fetch is not None and last_fetch.status not in _TERMINAL:
            watch.next_check_at = now + timedelta(seconds=watch.interval_seconds)
            continue

        if last_fetch is not None and last_fetch.status == JobStatus.DEAD_LETTER:
            watch.consecutive_failures += 1
            if watch.consecutive_failures >= BROKEN_AFTER_CONSECUTIVE_FAILURES:
                watch.state = WatchState.BROKEN
                watch.is_active = False
                await create_alert(
                    db,
                    watch,
                    check_id=None,
                    kind=WatchAlertKind.BROKEN,
                    message=(
                        f"Watch '{watch.name}' deactivated: checks failed "
                        f"{watch.consecutive_failures} times in a row (last error dead-lettered). "
                        f"Fix the target or the URL, then resume the watch."
                    ),
                    dedupe_key=f"watch:{watch.id}:broken:{watch.last_fetch_job_id}",
                )
                logger.warning("Watch %s marked broken after %s consecutive failures", watch.id, watch.consecutive_failures)
                continue

        queue = await db.get(Queue, watch.queue_id)
        if queue is None:
            watch.is_active = False
            continue

        fetch = await create_job(
            db, queue,
            name="watch_fetch",
            job_type=JobType.IMMEDIATE,
            payload={"watch_id": watch.id},
            timeout_seconds=CHECK_TIMEOUT_SECONDS,
        )
        # The fetch handler stamps its WatchCheck row with its own job id
        # (that's how diff finds the tick's check); a job doesn't know its
        # id until flushed, so patch it into the payload now.
        fetch.payload = {"watch_id": watch.id, "fetch_job_id": fetch.id}
        diff = await create_job(
            db, queue,
            name="watch_diff",
            job_type=JobType.IMMEDIATE,
            payload={"watch_id": watch.id, "fetch_job_id": fetch.id},
            depends_on=[fetch.id],
            timeout_seconds=CHECK_TIMEOUT_SECONDS,
        )
        await create_job(
            db, queue,
            name="watch_notify",
            job_type=JobType.IMMEDIATE,
            payload={"watch_id": watch.id, "diff_job_id": diff.id},
            depends_on=[diff.id],
            timeout_seconds=CHECK_TIMEOUT_SECONDS,
        )

        watch.last_fetch_job_id = fetch.id
        watch.last_check_at = now
        watch.next_check_at = now + timedelta(seconds=watch.interval_seconds)
        materialized += 1

    await db.flush()
    return materialized
