import asyncio
import logging
import os
import socket
import time
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models import Job, JobExecution, JobStatus, Queue, RetryPolicy, Worker, WorkerHeartbeat, WorkerStatus
from app.services.job_service import claim_jobs, complete_execution, fail_execution, start_execution
from worker.handlers import get_handler
from worker.naming import generate_worker_name

logger = logging.getLogger("jobsched.worker")
settings = get_settings()


async def run_handler_with_timeout(handler, payload: dict, timeout_seconds: float | None):
    """Run a job handler, enforcing the job's per-execution wall-clock
    limit. asyncio.wait_for cancels the handler coroutine on expiry, and
    the raised error carries a readable message so the execution record /
    retry log says what actually happened instead of a blank TimeoutError.
    """
    if not timeout_seconds:
        return await handler(payload)
    try:
        return await asyncio.wait_for(handler(payload), timeout=timeout_seconds)
    except TimeoutError:
        raise RuntimeError(f"Execution timed out after {timeout_seconds:g}s") from None


class WorkerRunner:
    """A single worker process.

    Concurrency model: one asyncio event loop, one semaphore capping the
    number of jobs this process runs at once (`concurrency`). Claiming is
    cluster-aware -- before claiming from a queue we subtract that queue's
    currently-RUNNING/CLAIMED count (across *all* workers) from its
    max_concurrency, so the queue's configured limit applies across the
    whole cluster rather than per-worker.

    That cluster-wide cap is enforced **best-effort, not strictly**: the
    capacity COUNT in `_claimable_capacity` and the claim in `claim_jobs`
    are two separate statements, so N workers polling simultaneously can
    each read the same free capacity and each claim against it. Overshoot
    is bounded by (concurrent workers x per-poll claim limit), never
    unbounded, and self-corrects on the next poll. A strict cap would need
    a per-queue advisory lock or a counter row -- see "Known limitations"
    in docs/DESIGN_DECISIONS.md for why that's deliberately out of scope.
    """

    def __init__(self, concurrency: int | None = None):
        self.concurrency = concurrency or settings.worker_default_concurrency
        self.semaphore = asyncio.Semaphore(self.concurrency)
        self.worker_id: int | None = None
        self._shutdown = asyncio.Event()
        self._draining = False
        self._in_flight: set[asyncio.Task] = set()

    async def register(self) -> None:
        async with AsyncSessionLocal() as db:
            worker = Worker(
                name=generate_worker_name(),
                hostname=socket.gethostname(),
                pid=os.getpid(),
                status=WorkerStatus.ONLINE,
                concurrency=self.concurrency,
            )
            db.add(worker)
            await db.commit()
            await db.refresh(worker)
            self.worker_id = worker.id
            logger.info("Registered worker id=%s pid=%s concurrency=%s", worker.id, worker.pid, self.concurrency)

    async def deregister(self) -> None:
        if self.worker_id is None:
            return
        async with AsyncSessionLocal() as db:
            worker = await db.get(Worker, self.worker_id)
            if worker:
                worker.status = WorkerStatus.OFFLINE
                await db.commit()
        logger.info("Worker %s marked offline", self.worker_id)

    def request_shutdown(self) -> None:
        logger.info("Shutdown requested; draining in-flight jobs (up to %s)", len(self._in_flight))
        self._draining = True
        self._shutdown.set()

    async def _heartbeat_loop(self) -> None:
        while not self._shutdown.is_set():
            async with AsyncSessionLocal() as db:
                worker = await db.get(Worker, self.worker_id)
                if worker:
                    worker.last_seen_at = datetime.now(timezone.utc)
                    worker.status = WorkerStatus.DRAINING if self._draining else WorkerStatus.ONLINE
                    db.add(WorkerHeartbeat(
                        worker_id=self.worker_id,
                        active_job_count=len(self._in_flight),
                        metrics={"concurrency": self.concurrency},
                    ))
                    await db.commit()
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=settings.worker_heartbeat_interval_seconds)
            except asyncio.TimeoutError:
                pass

    async def _claimable_capacity(self, db, queue: Queue) -> int:
        in_flight = await db.execute(
            select(func.count(Job.id)).where(
                Job.queue_id == queue.id, Job.status.in_([JobStatus.CLAIMED, JobStatus.RUNNING])
            )
        )
        return max(queue.max_concurrency - (in_flight.scalar() or 0), 0)

    async def _poll_once(self) -> list[Job]:
        # Derived from the in-flight task set rather than the semaphore's
        # private `_value`. Besides not reaching into a private attribute,
        # this is the more accurate number: `_value` only drops once a task
        # actually acquires the semaphore, so a task that's been created but
        # is still waiting for a slot wouldn't be counted, and we'd claim
        # work we have no capacity for.
        free_capacity = self.concurrency - len(self._in_flight)
        if free_capacity <= 0:
            return []

        claimed: list[Job] = []
        async with AsyncSessionLocal() as db:
            queues_result = await db.execute(select(Queue).where(Queue.is_paused.is_(False)))
            queues = list(queues_result.scalars().all())
            queues.sort(key=lambda q: q.priority, reverse=True)

            for queue in queues:
                if free_capacity <= 0:
                    break
                capacity = min(await self._claimable_capacity(db, queue), free_capacity)
                if capacity <= 0:
                    continue
                jobs = await claim_jobs(db, self.worker_id, [queue.id], capacity)
                claimed.extend(jobs)
                free_capacity -= len(jobs)

            await db.commit()
        return claimed

    async def _execute_job(self, job_id: int) -> None:
        async with self.semaphore:
            # Phase 1: mark RUNNING, snapshot everything we need as plain
            # values so nothing touches a detached ORM instance later.
            async with AsyncSessionLocal() as db:
                job = await db.get(Job, job_id)
                if job is None:
                    # The job was claimed, then its queue (or project/org)
                    # was deleted before we got here -- ON DELETE CASCADE
                    # took the row with it. Nothing to run and nothing to
                    # record against; without this guard the next line
                    # raises AttributeError on None.
                    logger.warning("Job %s no longer exists (cascade-deleted after claim); skipping", job_id)
                    return

                execution = await start_execution(db, job, self.worker_id)
                execution_id = execution.id
                job_name, job_payload = job.name, dict(job.payload)
                job_timeout = job.timeout_seconds
                queue_result = await db.execute(
                    select(Queue).options(selectinload(Queue.retry_policy)).where(Queue.id == job.queue_id)
                )
                queue = queue_result.scalar_one_or_none()
                if queue is None:
                    logger.warning("Queue for job %s disappeared mid-start; skipping", job_id)
                    return
                retry_policy_id = queue.retry_policy.id if queue.retry_policy else None
                await db.commit()

            handler = get_handler(job_name)
            start = time.monotonic()
            try:
                result = await run_handler_with_timeout(handler, job_payload, job_timeout)
                async with AsyncSessionLocal() as db:
                    job = await db.get(Job, job_id)
                    execution = await db.get(JobExecution, execution_id)
                    await complete_execution(db, job, execution, result)
                    await db.commit()
                logger.info("Job %s completed in %.2fs", job_id, time.monotonic() - start)
            except Exception as exc:  # noqa: BLE001 - job handler errors must not crash the worker
                error = str(exc) or exc.__class__.__name__
                async with AsyncSessionLocal() as db:
                    job = await db.get(Job, job_id)
                    execution = await db.get(JobExecution, execution_id)
                    retry_policy = await db.get(RetryPolicy, retry_policy_id) if retry_policy_id else None
                    await fail_execution(db, job, execution, retry_policy, error)
                    await db.commit()
                logger.warning("Job %s failed: %s", job_id, error)

    async def run(self) -> None:
        await self.register()
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        try:
            while not self._draining:
                claimed = await self._poll_once()
                for job in claimed:
                    task = asyncio.create_task(self._execute_job(job.id))
                    self._in_flight.add(task)
                    task.add_done_callback(self._in_flight.discard)

                try:
                    await asyncio.wait_for(self._shutdown.wait(), timeout=settings.worker_poll_interval_seconds)
                except asyncio.TimeoutError:
                    pass

            # Draining: stop claiming, wait for in-flight jobs to finish.
            if self._in_flight:
                logger.info("Waiting for %s in-flight job(s) to finish", len(self._in_flight))
                await asyncio.gather(*list(self._in_flight), return_exceptions=True)
        finally:
            self._shutdown.set()
            await heartbeat_task
            await self.deregister()
