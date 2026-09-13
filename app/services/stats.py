from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CheckOutcome, ExecutionStatus, Job, JobExecution, JobStatus, WatchCheck
from app.timefmt import display_tz, to_display

# Window for the "current rate" metrics (jobs/sec, latency percentiles).
# Long enough to smooth out poll-interval noise, short enough that the
# dashboard reflects what the queue is doing *now* rather than averaging
# in yesterday's traffic.
RATE_WINDOW_SECONDS = 300


async def queue_latency_percentiles(db: AsyncSession, queue_id: int, since: datetime) -> dict:
    """p50/p95/p99 of successful execution durations since `since`.

    Measured over JobExecution.duration_ms (per-attempt handler runtime as
    recorded by the worker) rather than Job.completed_at - Job.started_at,
    so a job that succeeded on its 3rd attempt contributes its final
    attempt's real duration, not the whole span including backoff waits.
    percentile_cont is a Postgres ordered-set aggregate -- one round trip
    computes all three.
    """
    result = await db.execute(
        select(
            func.percentile_cont(0.5).within_group(JobExecution.duration_ms),
            func.percentile_cont(0.95).within_group(JobExecution.duration_ms),
            func.percentile_cont(0.99).within_group(JobExecution.duration_ms),
        )
        .join(Job, Job.id == JobExecution.job_id)
        .where(
            Job.queue_id == queue_id,
            JobExecution.status == ExecutionStatus.SUCCEEDED,
            JobExecution.duration_ms.isnot(None),
            JobExecution.finished_at >= since,
        )
    )
    p50, p95, p99 = result.one()
    return {
        "p50_ms": float(p50) if p50 is not None else None,
        "p95_ms": float(p95) if p95 is not None else None,
        "p99_ms": float(p99) if p99 is not None else None,
    }


async def queue_stats(db: AsyncSession, queue_id: int) -> dict:
    result = await db.execute(
        select(Job.status, func.count(Job.id)).where(Job.queue_id == queue_id).group_by(Job.status)
    )
    counts = {status.value: 0 for status in JobStatus}
    for status, count in result.all():
        counts[status.value] = count

    avg_duration = await db.execute(
        select(func.avg(func.extract("epoch", Job.completed_at - Job.started_at) * 1000)).where(
            Job.queue_id == queue_id, Job.status == JobStatus.COMPLETED, Job.started_at.isnot(None)
        )
    )
    avg_ms = avg_duration.scalar()

    now = datetime.now(timezone.utc)
    since_hour = now - timedelta(hours=1)
    throughput = await db.execute(
        select(func.count(Job.id)).where(
            Job.queue_id == queue_id, Job.status == JobStatus.COMPLETED, Job.completed_at >= since_hour
        )
    )

    since_rate = now - timedelta(seconds=RATE_WINDOW_SECONDS)
    recent_completed = await db.execute(
        select(func.count(Job.id)).where(
            Job.queue_id == queue_id, Job.status == JobStatus.COMPLETED, Job.completed_at >= since_rate
        )
    )
    jobs_per_second = (recent_completed.scalar() or 0) / RATE_WINDOW_SECONDS

    percentiles = await queue_latency_percentiles(db, queue_id, since_rate)

    return {
        "queue_id": queue_id,
        "queued": counts.get("queued", 0),
        "scheduled": counts.get("scheduled", 0),
        "blocked": counts.get("blocked", 0),
        "claimed": counts.get("claimed", 0),
        "running": counts.get("running", 0),
        "completed": counts.get("completed", 0),
        "failed": counts.get("failed", 0) + counts.get("retrying", 0),
        "dead_letter": counts.get("dead_letter", 0),
        "cancelled": counts.get("cancelled", 0),
        "avg_duration_ms": float(avg_ms) if avg_ms is not None else None,
        "throughput_last_hour": throughput.scalar() or 0,
        "jobs_per_second": jobs_per_second,
        **percentiles,
    }


async def watch_stats(db: AsyncSession, watch_id: int) -> dict:
    """Headline numbers for a watch's detail page: lifetime and 24h check
    counts, 24h ok-rate, and 24h latency percentiles (percentile_cont over
    recorded check latencies)."""
    total = (
        await db.execute(select(func.count(WatchCheck.id)).where(WatchCheck.watch_id == watch_id))
    ).scalar_one()

    since = datetime.now(timezone.utc) - timedelta(hours=24)
    row = (
        await db.execute(
            select(
                func.count(WatchCheck.id),
                func.count(WatchCheck.id).filter(WatchCheck.outcome == CheckOutcome.OK),
                func.percentile_cont(0.5).within_group(WatchCheck.latency_ms),
                func.percentile_cont(0.95).within_group(WatchCheck.latency_ms),
            ).where(WatchCheck.watch_id == watch_id, WatchCheck.started_at >= since)
        )
    ).one()
    checks_24h, ok_24h, p50, p95 = row

    last_latency = (
        await db.execute(
            select(WatchCheck.latency_ms)
            .where(WatchCheck.watch_id == watch_id, WatchCheck.latency_ms.isnot(None))
            .order_by(WatchCheck.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    return {
        "watch_id": watch_id,
        "total_checks": total,
        "checks_24h": checks_24h,
        "ok_pct_24h": (100.0 * ok_24h / checks_24h) if checks_24h else None,
        "p50_latency_ms": float(p50) if p50 is not None else None,
        "p95_latency_ms": float(p95) if p95 is not None else None,
        "last_latency_ms": last_latency,
    }


async def watch_latency_series(db: AsyncSession, watch_id: int, limit: int = 50) -> dict:
    """Chronological (time label, latency, outcome) triples for the watch
    detail page's latency chart -- parallel lists, chart-ready."""
    rows = (
        await db.execute(
            select(WatchCheck.started_at, WatchCheck.latency_ms, WatchCheck.outcome)
            .where(WatchCheck.watch_id == watch_id)
            .order_by(WatchCheck.started_at.desc())
            .limit(limit)
        )
    ).all()
    rows.reverse()
    return {
        "labels": [to_display(r[0]).strftime("%H:%M:%S") for r in rows],
        "latency_ms": [r[1] for r in rows],
        "outcomes": [(r[2].value if r[2] else "error") for r in rows],
    }


async def queue_health_series(db: AsyncSession, queue_id: int, hours: int = 24) -> dict:
    """Hourly-bucketed completed vs. dead-lettered counts for the last
    `hours` hours, for the queue detail page's throughput/health chart.
    Returns parallel lists so the template can hand them straight to
    Chart.js without any further transformation.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    # Bucket on wall-clock hours in the display zone, so the chart's labels
    # read 18:00, 19:00 ... rather than UTC hours shifted by +05:30.
    # timezone(zone, timestamptz) yields a naive local timestamp in Postgres,
    # which is why the keys below are naive local datetimes too. Zones with
    # DST are best-effort on transition days; the default has none.
    # display_tz() is the validated zone (falls back to UTC on a bad name), so
    # the SQL side can never disagree with the Python keys built below.
    tz_name = display_tz().key
    bucket = func.date_trunc("hour", func.timezone(tz_name, Job.completed_at))

    result = await db.execute(
        select(bucket.label("hour"), Job.status, func.count(Job.id))
        .where(Job.queue_id == queue_id, Job.completed_at >= since, Job.status.in_([JobStatus.COMPLETED, JobStatus.DEAD_LETTER]))
        .group_by("hour", Job.status)
        .order_by("hour")
    )

    completed_by_hour: dict[datetime, int] = {}
    dead_letter_by_hour: dict[datetime, int] = {}
    for hour, status, count in result.all():
        if status == JobStatus.COMPLETED:
            completed_by_hour[hour] = count
        else:
            dead_letter_by_hour[hour] = count

    now = datetime.now(display_tz()).replace(tzinfo=None, minute=0, second=0, microsecond=0)
    labels, completed, dead_lettered = [], [], []
    for i in range(hours - 1, -1, -1):
        hour = now - timedelta(hours=i)
        labels.append(hour.strftime("%H:%M"))
        completed.append(completed_by_hour.get(hour, 0))
        dead_lettered.append(dead_letter_by_hour.get(hour, 0))

    return {"labels": labels, "completed": completed, "dead_lettered": dead_lettered}
