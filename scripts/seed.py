"""Populate the database with realistic-looking demo data: several
projects, queues with varied config, workers, scheduled jobs, and jobs
spread across every status (completed, running, queued, retrying,
dead_letter, scheduled) with execution history and logs -- so the
dashboard looks like a system that's actually been in use, not an empty
shell right after signup.

Usage (run inside the api container, against the real app database):

    docker compose exec api python -m scripts.seed

Attaches everything to the first Organization it finds (i.e. the org you
created when you registered), so log in with your existing account and
you'll see the seeded projects/queues/jobs immediately. Safe to re-run --
it skips projects that already exist by name instead of duplicating them.
"""
import asyncio
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import (
    DeadLetterEntry,
    ExecutionStatus,
    Job,
    JobExecution,
    JobLog,
    JobStatus,
    JobType,
    LogLevel,
    Organization,
    Project,
    Queue,
    RetryPolicy,
    RetryStrategy,
    ScheduledJob,
    Worker,
    WorkerHeartbeat,
    WorkerStatus,
)
from app.services.job_service import create_job

random.seed(7)
NOW = datetime.now(timezone.utc)

PROJECTS = [
    {
        "name": "Payments Platform",
        "description": "Billing, invoicing, and webhook delivery for the payments team.",
        "queues": [
            {
                "name": "webhook-delivery",
                "priority": 5,
                "max_concurrency": 8,
                "strategy": RetryStrategy.EXPONENTIAL,
                "max_retries": 6,
                "base_delay_seconds": 1.0,
                "jobs": ["deliver-webhook-stripe", "deliver-webhook-github", "deliver-webhook-slack", "retry-failed-webhook"],
            },
            {
                "name": "invoice-generation",
                "priority": 2,
                "max_concurrency": 4,
                "strategy": RetryStrategy.FIXED,
                "max_retries": 3,
                "base_delay_seconds": 10.0,
                "jobs": ["generate-invoice", "send-invoice-email", "calculate-tax", "apply-discount-code"],
            },
        ],
    },
    {
        "name": "Notifications Service",
        "description": "Transactional email and push notifications across all products.",
        "queues": [
            {
                "name": "email-notifications",
                "priority": 3,
                "max_concurrency": 10,
                "strategy": RetryStrategy.EXPONENTIAL,
                "max_retries": 5,
                "base_delay_seconds": 2.0,
                "jobs": ["send-welcome-email", "send-password-reset", "send-weekly-digest", "send-receipt-email"],
            },
            {
                "name": "push-notifications",
                "priority": 3,
                "max_concurrency": 10,
                "strategy": RetryStrategy.LINEAR,
                "max_retries": 4,
                "base_delay_seconds": 3.0,
                "jobs": ["send-push-ios", "send-push-android", "send-order-update", "send-promo-alert"],
            },
        ],
    },
    {
        "name": "Data Pipeline",
        "description": "Nightly syncs, reconciliation, and reporting jobs.",
        "queues": [
            {
                "name": "data-sync",
                "priority": 1,
                "max_concurrency": 2,
                "strategy": RetryStrategy.EXPONENTIAL,
                "max_retries": 4,
                "base_delay_seconds": 5.0,
                "jobs": ["sync-inventory", "sync-customer-records", "sync-crm-contacts", "reconcile-ledger"],
            },
            {
                "name": "report-generation",
                "priority": 0,
                "max_concurrency": 2,
                "strategy": RetryStrategy.FIXED,
                "max_retries": 2,
                "base_delay_seconds": 30.0,
                "jobs": ["generate-monthly-report", "generate-usage-report", "export-csv", "compile-analytics"],
            },
        ],
    },
]

WORKERS = [
    {"name": "worker-a1", "hostname": "ip-10-0-1-14", "status": WorkerStatus.ONLINE, "last_seen_ago": 3},
    {"name": "worker-a2", "hostname": "ip-10-0-1-15", "status": WorkerStatus.ONLINE, "last_seen_ago": 8},
    {"name": "worker-b1", "hostname": "ip-10-0-2-9", "status": WorkerStatus.OFFLINE, "last_seen_ago": 3600},
    {"name": "worker-b2", "hostname": "ip-10-0-2-10", "status": WorkerStatus.OFFLINE, "last_seen_ago": 7200},
]

SCHEDULED_JOBS = [
    ("data-sync", "hourly-inventory-sync", "sync-inventory", "0 * * * *"),
    ("report-generation", "nightly-usage-report", "generate-usage-report", "0 2 * * *"),
    ("email-notifications", "weekly-digest-email", "send-weekly-digest", "0 9 * * 1"),
]


def _rand_time_within(hours_back: float) -> datetime:
    return NOW - timedelta(seconds=random.uniform(0, hours_back * 3600))


async def _get_or_create_worker(db, spec) -> Worker:
    existing = await db.execute(select(Worker).where(Worker.name == spec["name"], Worker.hostname == spec["hostname"]))
    found = existing.scalar_one_or_none()
    if found is not None:
        return found

    worker = Worker(
        name=spec["name"],
        hostname=spec["hostname"],
        pid=random.randint(100, 60000),
        status=spec["status"],
        concurrency=random.choice([4, 8]),
        last_seen_at=NOW - timedelta(seconds=spec["last_seen_ago"]),
    )
    db.add(worker)
    await db.flush()
    for i in range(3):
        db.add(WorkerHeartbeat(
            worker_id=worker.id,
            timestamp=worker.last_seen_at - timedelta(seconds=i * 5),
            active_job_count=random.randint(0, worker.concurrency),
        ))
    return worker


async def _seed_job(db, queue: Queue, name: str, status: JobStatus, workers: list[Worker]) -> None:
    job_type = JobType.IMMEDIATE
    run_at = NOW
    completed_at = None
    started_at = None
    claimed_by = None
    attempt_count = 0

    if status == JobStatus.COMPLETED:
        started_at = _rand_time_within(48)
        completed_at = started_at + timedelta(milliseconds=random.randint(120, 4000))
        run_at = started_at
        claimed_by = random.choice(workers).id
        attempt_count = 1
    elif status == JobStatus.QUEUED:
        run_at = NOW - timedelta(seconds=random.randint(0, 30))
    elif status == JobStatus.SCHEDULED:
        job_type = JobType.DELAYED
        run_at = NOW + timedelta(minutes=random.randint(5, 120))
    elif status == JobStatus.RUNNING:
        started_at = NOW - timedelta(seconds=random.randint(1, 20))
        run_at = started_at
        claimed_by = random.choice(workers).id
        attempt_count = 1
    elif status == JobStatus.RETRYING:
        attempt_count = random.randint(1, 2)
        run_at = _rand_time_within(6)
    elif status == JobStatus.DEAD_LETTER:
        attempt_count = queue.retry_policy.max_retries + 1
        run_at = _rand_time_within(24)
        completed_at = run_at + timedelta(minutes=5)

    job = Job(
        queue_id=queue.id,
        name=name,
        job_type=job_type,
        status=status,
        payload={"seed": True},
        run_at=run_at,
        attempt_count=attempt_count,
        claimed_by=claimed_by,
        claimed_at=started_at,
        started_at=started_at,
        completed_at=completed_at,
        next_retry_at=NOW + timedelta(seconds=random.randint(5, 120)) if status == JobStatus.RETRYING else None,
    )
    db.add(job)
    await db.flush()

    if status == JobStatus.COMPLETED:
        exec_ = JobExecution(
            job_id=job.id, attempt_number=1, worker_id=claimed_by, status=ExecutionStatus.SUCCEEDED,
            started_at=started_at, finished_at=completed_at,
            duration_ms=int((completed_at - started_at).total_seconds() * 1000),
            result={"ok": True},
        )
        db.add(exec_)
        db.add(JobLog(job_id=job.id, level=LogLevel.INFO, message="Job completed successfully", timestamp=completed_at))

    elif status == JobStatus.RUNNING:
        db.add(JobExecution(job_id=job.id, attempt_number=1, worker_id=claimed_by, status=ExecutionStatus.RUNNING, started_at=started_at))
        db.add(JobLog(job_id=job.id, level=LogLevel.INFO, message="Job claimed and started", timestamp=started_at))

    elif status == JobStatus.RETRYING:
        for attempt in range(1, attempt_count + 1):
            t = run_at - timedelta(minutes=(attempt_count - attempt + 1) * 2)
            db.add(JobExecution(
                job_id=job.id, attempt_number=attempt, worker_id=random.choice(workers).id,
                status=ExecutionStatus.FAILED, started_at=t, finished_at=t + timedelta(milliseconds=300),
                duration_ms=300, error="Connection timed out",
            ))
            db.add(JobLog(job_id=job.id, level=LogLevel.ERROR, message=f"Attempt {attempt} failed: Connection timed out", timestamp=t))
        db.add(JobLog(job_id=job.id, level=LogLevel.WARNING, message="Retry scheduled", timestamp=run_at))

    elif status == JobStatus.DEAD_LETTER:
        for attempt in range(1, attempt_count + 1):
            t = run_at - timedelta(minutes=(attempt_count - attempt + 1) * 3)
            db.add(JobExecution(
                job_id=job.id, attempt_number=attempt, worker_id=random.choice(workers).id,
                status=ExecutionStatus.FAILED, started_at=t, finished_at=t + timedelta(milliseconds=500),
                duration_ms=500, error="Upstream service returned 503",
            ))
        db.add(JobLog(job_id=job.id, level=LogLevel.ERROR, message="Max retries exhausted; moved to dead letter queue", timestamp=completed_at))
        db.add(DeadLetterEntry(
            job_id=job.id, queue_id=queue.id, reason="Upstream service returned 503",
            attempt_count=attempt_count, payload_snapshot=job.payload, failed_at=completed_at,
        ))


async def main() -> None:
    async with AsyncSessionLocal() as db:
        org_result = await db.execute(select(Organization).order_by(Organization.id).limit(1))
        org = org_result.scalar_one_or_none()
        if org is None:
            print("No organization found -- register an account at /register first, then re-run this script.")
            return

        workers = [await _get_or_create_worker(db, spec) for spec in WORKERS]
        await db.flush()

        queues_by_name: dict[str, Queue] = {}

        for project_spec in PROJECTS:
            existing = await db.execute(
                select(Project).where(Project.organization_id == org.id, Project.name == project_spec["name"])
            )
            if existing.scalar_one_or_none() is not None:
                print(f"Skipping project {project_spec['name']!r} -- already exists")
                continue

            project = Project(organization_id=org.id, name=project_spec["name"], description=project_spec["description"])
            db.add(project)
            await db.flush()

            for queue_spec in project_spec["queues"]:
                queue = Queue(
                    project_id=project.id, name=queue_spec["name"],
                    priority=queue_spec["priority"], max_concurrency=queue_spec["max_concurrency"],
                )
                db.add(queue)
                await db.flush()
                retry_policy = RetryPolicy(
                    queue_id=queue.id, strategy=queue_spec["strategy"],
                    max_retries=queue_spec["max_retries"], base_delay_seconds=queue_spec["base_delay_seconds"],
                )
                db.add(retry_policy)
                await db.flush()
                queue.retry_policy = retry_policy
                queues_by_name[queue.name] = queue

                status_plan = (
                    [JobStatus.COMPLETED] * 12
                    + [JobStatus.QUEUED] * 3
                    + [JobStatus.RUNNING] * 1
                    + [JobStatus.RETRYING] * 2
                    + [JobStatus.DEAD_LETTER] * 1
                    + [JobStatus.SCHEDULED] * 1
                )
                for status in status_plan:
                    name = random.choice(queue_spec["jobs"])
                    await _seed_job(db, queue, name, status, workers)

            print(f"Seeded project {project_spec['name']!r}")

        for queue_name, sj_name, job_name, cron in SCHEDULED_JOBS:
            queue = queues_by_name.get(queue_name)
            if queue is None:
                continue
            db.add(ScheduledJob(
                queue_id=queue.id, name=sj_name, job_name=job_name,
                payload_template={"seed": True}, cron_expression=cron, is_recurring=True,
                next_run_at=NOW + timedelta(hours=random.randint(1, 12)),
                last_run_at=NOW - timedelta(hours=random.randint(1, 24)),
            ))

        # Beyond the historical/backfilled jobs above, submit a handful of
        # genuinely fresh ones through the real create_job() path (the same
        # code the API uses) so they land as status=QUEUED and your actual
        # running worker picks them up and completes them live within a
        # few seconds -- something to watch happen, not just static rows.
        live_count = 0
        for queue_name in queues_by_name:
            queue = queues_by_name[queue_name]
            job_names = next(
                q["jobs"] for p in PROJECTS for q in p["queues"] if q["name"] == queue_name
            )
            for name in random.sample(job_names, k=min(2, len(job_names))):
                await create_job(
                    db, queue, name=name, job_type=JobType.IMMEDIATE,
                    payload={"seed": True, "live_demo": True},
                )
                live_count += 1

        await db.commit()
        print(f"Done. Submitted {live_count} fresh jobs your worker will pick up live -- check the dashboard in a few seconds.")


if __name__ == "__main__":
    asyncio.run(main())
