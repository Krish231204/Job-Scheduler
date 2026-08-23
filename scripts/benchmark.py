"""Reproducible end-to-end benchmark for the job scheduler.

Runs a fixed, deterministic workload against a dedicated benchmark database
and prints submission throughput, drain throughput (jobs/sec), and per-job
latency percentiles (p50/p95/p99) -- so a configuration change (worker pool
size, per-worker concurrency, an index, a dependency bump) can be quoted as
a real before/after number instead of a feeling.

What it measures, in order:

1. **Submission**: `create_job` for N immediate jobs, one commit per job
   (mirroring one API request per submission). With `--idempotency-keys`
   (default on) every job carries a unique key, so this path exercises the
   partial unique index from migration 0003 exactly like the API does.
2. **Drain**: starts W real `WorkerRunner` instances (the actual worker
   process code: claim via SELECT ... FOR UPDATE SKIP LOCKED, execute,
   record executions/logs) with per-worker concurrency C, and times how
   long the pool takes to complete all N jobs.
3. **Latency**: computed in SQL over the drained jobs --
   - *execution*: JobExecution.duration_ms (handler runtime as recorded
     by the worker, i.e. per-attempt cost with bookkeeping excluded);
   - *end-to-end*: completed_at - created_at (submission to completion,
     including time spent waiting in the queue; for a pre-loaded queue
     this mostly reflects drain order, which is why both are reported).

Reproducibility rules: no randomness anywhere, fixed payloads, sequential
idempotency keys, and a pinned worker poll interval (--poll-interval,
default 0.05s -- printed with the results; the production default of 1.0s
would make poll latency, not the scheduler, the thing being measured).

Usage:

    # Postgres must be reachable; the bench database is created if missing.
    python -m scripts.benchmark                       # defaults: 500 jobs, 1 worker x 8
    python -m scripts.benchmark --jobs 1000 --workers 4 --concurrency 8
    python -m scripts.benchmark --handler-ms 20       # simulate real work per job
    python -m scripts.benchmark --json                # machine-readable output

    BENCH_DATABASE_URL=postgresql+asyncpg://user:pw@host:5432/jobsched_bench \
        python -m scripts.benchmark

The benchmark database is truncated at the start of every run. Never point
BENCH_DATABASE_URL at a database you care about.
"""
import argparse
import asyncio
import json
import logging
import os
import platform
import time

# The worker logs one line per claim/completion; at benchmark volumes that
# is thousands of lines drowning out the report.
logging.getLogger("jobsched.worker").setLevel(logging.WARNING)

DEFAULT_BENCH_URL = "postgresql+asyncpg://jobsched:jobsched@localhost:5432/jobsched_bench"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Job scheduler benchmark (fixed, reproducible workload)")
    parser.add_argument("--jobs", type=int, default=500, help="Number of jobs in the fixed workload (default 500)")
    parser.add_argument("--workers", type=int, default=1, help="Worker processes to simulate, i.e. WorkerRunner instances (default 1)")
    parser.add_argument("--concurrency", type=int, default=8, help="Per-worker concurrency (default 8)")
    parser.add_argument("--handler-ms", type=int, default=0, help="Fixed simulated work per job in ms (default 0 = pure scheduler overhead)")
    parser.add_argument("--poll-interval", type=float, default=0.05, help="Worker poll interval in seconds (default 0.05)")
    parser.add_argument("--no-idempotency-keys", dest="idempotency_keys", action="store_false",
                        help="Submit without idempotency keys (skips the partial-unique-index lookup path)")
    parser.add_argument("--json", action="store_true", help="Print results as JSON instead of the human-readable report")
    return parser.parse_args()


ARGS = _parse_args()

# Must happen before any `app.` / `worker.` import: app.config reads the
# environment once (lru_cache) and app.database builds its engine from it.
os.environ["DATABASE_URL"] = os.environ.get("BENCH_DATABASE_URL", DEFAULT_BENCH_URL)
os.environ["WORKER_POLL_INTERVAL_SECONDS"] = str(ARGS.poll_interval)

from sqlalchemy import create_engine, func, select, text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.database import AsyncSessionLocal, Base  # noqa: E402
from app.models import (  # noqa: E402
    ExecutionStatus,
    Job,
    JobExecution,
    JobStatus,
    JobType,
    Organization,
    Project,
    Queue,
    RetryPolicy,
)
from app.services.job_service import create_job  # noqa: E402
from worker.handlers import register  # noqa: E402
from worker.runner import WorkerRunner  # noqa: E402

settings = get_settings()

BENCH_JOB_NAME = "bench_fixed_work"


@register(BENCH_JOB_NAME)
async def _bench_handler(payload: dict) -> dict:
    work_ms = int(payload.get("work_ms", 0))
    if work_ms:
        await asyncio.sleep(work_ms / 1000)
    return {"ok": True}


def _ensure_database_exists(url: str) -> None:
    """Create the bench database if it doesn't exist (idempotent)."""
    db_name = url.rsplit("/", 1)[1].split("?")[0]
    admin_url = url.rsplit("/", 1)[0] + "/postgres"
    sync_admin = admin_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    engine = create_engine(sync_admin, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        exists = conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": db_name}).scalar()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    engine.dispose()


async def _reset_schema(url: str) -> None:
    # Drop + recreate rather than truncate: the bench DB may hold a stale
    # schema from an older code revision, and create_all skips existing
    # tables.
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()


async def _make_bench_queue() -> int:
    async with AsyncSessionLocal() as db:
        org = Organization(name="bench-org")
        db.add(org)
        await db.flush()
        project = Project(organization_id=org.id, name="bench-project")
        db.add(project)
        await db.flush()
        queue = Queue(project_id=project.id, name="bench-queue", priority=0, max_concurrency=10_000)
        db.add(queue)
        await db.flush()
        db.add(RetryPolicy(queue_id=queue.id, max_retries=0))
        await db.commit()
        return queue.id


async def _submit_jobs(queue_id: int, n: int, handler_ms: int, use_keys: bool) -> float:
    """Submit n immediate jobs, one commit each (the API-request pattern).
    Returns elapsed seconds."""
    start = time.perf_counter()
    async with AsyncSessionLocal() as db:
        queue = await db.get(Queue, queue_id)
        for i in range(n):
            await create_job(
                db,
                queue,
                name=BENCH_JOB_NAME,
                job_type=JobType.IMMEDIATE,
                payload={"work_ms": handler_ms, "seq": i},
                idempotency_key=f"bench-{i:08d}" if use_keys else None,
            )
            await db.commit()
    return time.perf_counter() - start


async def _drain(queue_id: int, n: int, workers: int, concurrency: int) -> float:
    """Start the worker pool, wait until all n jobs are completed, shut the
    pool down. Returns elapsed seconds from pool start to last completion."""
    runners = [WorkerRunner(concurrency=concurrency) for _ in range(workers)]
    start = time.perf_counter()
    tasks = [asyncio.create_task(r.run()) for r in runners]

    async with AsyncSessionLocal() as db:
        while True:
            done = await db.execute(
                select(func.count(Job.id)).where(Job.queue_id == queue_id, Job.status == JobStatus.COMPLETED)
            )
            done_count = done.scalar() or 0
            await db.commit()  # release the pooled connection between polls
            if done_count >= n:
                break
            await asyncio.sleep(0.05)
    elapsed = time.perf_counter() - start

    for r in runners:
        r.request_shutdown()
    await asyncio.gather(*tasks)
    return elapsed


async def _latency_percentiles(queue_id: int) -> dict:
    async with AsyncSessionLocal() as db:
        exec_result = await db.execute(
            select(
                func.percentile_cont(0.5).within_group(JobExecution.duration_ms),
                func.percentile_cont(0.95).within_group(JobExecution.duration_ms),
                func.percentile_cont(0.99).within_group(JobExecution.duration_ms),
            )
            .join(Job, Job.id == JobExecution.job_id)
            .where(Job.queue_id == queue_id, JobExecution.status == ExecutionStatus.SUCCEEDED)
        )
        exec_p50, exec_p95, exec_p99 = exec_result.one()

        e2e_ms = func.extract("epoch", Job.completed_at - Job.created_at) * 1000
        e2e_result = await db.execute(
            select(
                func.percentile_cont(0.5).within_group(e2e_ms),
                func.percentile_cont(0.95).within_group(e2e_ms),
                func.percentile_cont(0.99).within_group(e2e_ms),
            ).where(Job.queue_id == queue_id, Job.status == JobStatus.COMPLETED)
        )
        e2e_p50, e2e_p95, e2e_p99 = e2e_result.one()

    return {
        "execution_ms": {"p50": float(exec_p50), "p95": float(exec_p95), "p99": float(exec_p99)},
        "end_to_end_ms": {"p50": float(e2e_p50), "p95": float(e2e_p95), "p99": float(e2e_p99)},
    }


async def main() -> None:
    url = settings.database_url
    _ensure_database_exists(url)
    await _reset_schema(url)
    queue_id = await _make_bench_queue()

    submit_seconds = await _submit_jobs(queue_id, ARGS.jobs, ARGS.handler_ms, ARGS.idempotency_keys)
    drain_seconds = await _drain(queue_id, ARGS.jobs, ARGS.workers, ARGS.concurrency)
    latency = await _latency_percentiles(queue_id)

    results = {
        "config": {
            "jobs": ARGS.jobs,
            "workers": ARGS.workers,
            "concurrency_per_worker": ARGS.concurrency,
            "handler_ms": ARGS.handler_ms,
            "poll_interval_seconds": ARGS.poll_interval,
            "idempotency_keys": ARGS.idempotency_keys,
            "python": platform.python_version(),
            "cpus": os.cpu_count(),
        },
        "submission": {
            "seconds": round(submit_seconds, 3),
            "jobs_per_second": round(ARGS.jobs / submit_seconds, 1),
        },
        "drain": {
            "seconds": round(drain_seconds, 3),
            "jobs_per_second": round(ARGS.jobs / drain_seconds, 1),
        },
        "latency": {
            metric: {k: round(v, 1) for k, v in values.items()}
            for metric, values in latency.items()
        },
    }

    if ARGS.json:
        print(json.dumps(results, indent=2))
        return

    c, s, d, lat = results["config"], results["submission"], results["drain"], results["latency"]
    print(f"""
Job scheduler benchmark  (Python {c['python']}, {c['cpus']} CPUs)
================================================================
Workload    {c['jobs']} immediate jobs, handler {c['handler_ms']}ms, \
idempotency keys {'on' if c['idempotency_keys'] else 'off'}
Worker pool {c['workers']} worker(s) x concurrency {c['concurrency_per_worker']}, \
poll interval {c['poll_interval_seconds']}s

Submission  {s['jobs_per_second']:>8.1f} jobs/sec   ({s['seconds']}s for {c['jobs']} jobs, one commit per job)
Drain       {d['jobs_per_second']:>8.1f} jobs/sec   ({d['seconds']}s from pool start to last completion)

Latency (ms)          p50        p95        p99
  execution      {lat['execution_ms']['p50']:>8.1f}   {lat['execution_ms']['p95']:>8.1f}   {lat['execution_ms']['p99']:>8.1f}
  end-to-end     {lat['end_to_end_ms']['p50']:>8.1f}   {lat['end_to_end_ms']['p95']:>8.1f}   {lat['end_to_end_ms']['p99']:>8.1f}

Execution = handler runtime per successful attempt, as recorded by the worker.
End-to-end = submission to completion, including queue wait; with the whole
workload pre-loaded this mostly reflects drain order -- compare it across
configs, don't read it as user-facing latency.
""")


if __name__ == "__main__":
    asyncio.run(main())
