"""Tests for the queue metrics in app/services/stats.py -- specifically the
latency percentiles and jobs/sec rate added for the performance work, since
those back both the dashboard tiles and the benchmark's dashboard-visible
numbers. Needs real Postgres: percentile_cont is an ordered-set aggregate
SQLite doesn't implement.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import JobExecution, JobStatus, JobType, Worker, WorkerStatus
from app.services.job_service import claim_jobs, complete_execution, create_job, start_execution
from app.services.stats import RATE_WINDOW_SECONDS, queue_stats
from tests.conftest import requires_db
from tests.factories import make_queue


async def _run_job_with_duration(db, queue, worker, duration_ms: int):
    """Create -> claim -> start -> complete one job, then overwrite the
    recorded execution duration so percentile assertions are exact."""
    job = await create_job(db, queue, name="bench", job_type=JobType.IMMEDIATE, payload={})
    await claim_jobs(db, worker.id, [queue.id], limit=1)
    execution = await start_execution(db, job, worker.id)
    await complete_execution(db, job, execution, result={})
    execution.duration_ms = duration_ms
    await db.flush()
    return job


@requires_db
async def test_latency_percentiles_over_recent_executions(db_session):
    queue = await make_queue(db_session)
    worker = Worker(name="w", hostname="localhost", pid=1, status=WorkerStatus.ONLINE, concurrency=4)
    db_session.add(worker)
    await db_session.flush()

    # 100 executions with durations 1..100ms gives exact, easy-to-reason
    # percentiles: p50=50.5, p95=95.05, p99=99.01 (linear interpolation).
    for ms in range(1, 101):
        await _run_job_with_duration(db_session, queue, worker, ms)

    stats = await queue_stats(db_session, queue.id)
    assert stats["completed"] == 100
    assert abs(stats["p50_ms"] - 50.5) < 0.01
    assert abs(stats["p95_ms"] - 95.05) < 0.01
    assert abs(stats["p99_ms"] - 99.01) < 0.01


@requires_db
async def test_jobs_per_second_counts_only_recent_completions(db_session):
    queue = await make_queue(db_session)
    worker = Worker(name="w", hostname="localhost", pid=1, status=WorkerStatus.ONLINE, concurrency=4)
    db_session.add(worker)
    await db_session.flush()

    recent = await _run_job_with_duration(db_session, queue, worker, 10)
    old = await _run_job_with_duration(db_session, queue, worker, 10)
    # Push one completion (and its execution) outside the rate window; it
    # should still count toward lifetime totals but not the current rate.
    stale = datetime.now(timezone.utc) - timedelta(seconds=RATE_WINDOW_SECONDS + 60)
    old.completed_at = stale
    # Explicit query rather than `old.executions[0]`: lazy-loading a
    # relationship from test code raises MissingGreenlet under the async
    # ORM (same note as tests/test_lifecycle.py's _dlq_entry).
    execution_result = await db_session.execute(select(JobExecution).where(JobExecution.job_id == old.id))
    execution_result.scalar_one().finished_at = stale
    await db_session.flush()

    stats = await queue_stats(db_session, queue.id)
    assert recent.status == JobStatus.COMPLETED
    assert stats["completed"] == 2
    assert stats["jobs_per_second"] == 1 / RATE_WINDOW_SECONDS
    # Percentiles use the same window: only the recent execution remains.
    assert stats["p50_ms"] == 10.0


@requires_db
async def test_percentiles_are_none_with_no_recent_executions(db_session):
    queue = await make_queue(db_session)
    stats = await queue_stats(db_session, queue.id)
    assert stats["jobs_per_second"] == 0.0
    assert stats["p50_ms"] is None
    assert stats["p95_ms"] is None
    assert stats["p99_ms"] is None
