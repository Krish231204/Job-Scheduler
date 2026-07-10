from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Job, JobStatus


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

    since = datetime.now(timezone.utc) - timedelta(hours=1)
    throughput = await db.execute(
        select(func.count(Job.id)).where(
            Job.queue_id == queue_id, Job.status == JobStatus.COMPLETED, Job.completed_at >= since
        )
    )

    return {
        "queue_id": queue_id,
        "queued": counts.get("queued", 0),
        "scheduled": counts.get("scheduled", 0),
        "claimed": counts.get("claimed", 0),
        "running": counts.get("running", 0),
        "completed": counts.get("completed", 0),
        "failed": counts.get("failed", 0) + counts.get("retrying", 0),
        "dead_letter": counts.get("dead_letter", 0),
        "cancelled": counts.get("cancelled", 0),
        "avg_duration_ms": float(avg_ms) if avg_ms is not None else None,
        "throughput_last_hour": throughput.scalar() or 0,
    }


async def queue_health_series(db: AsyncSession, queue_id: int, hours: int = 24) -> dict:
    """Hourly-bucketed completed vs. dead-lettered counts for the last
    `hours` hours, for the queue detail page's throughput/health chart.
    Returns parallel lists so the template can hand them straight to
    Chart.js without any further transformation.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    bucket = func.date_trunc("hour", Job.completed_at)

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

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    labels, completed, dead_lettered = [], [], []
    for i in range(hours - 1, -1, -1):
        hour = now - timedelta(hours=i)
        labels.append(hour.strftime("%H:%M"))
        completed.append(completed_by_hour.get(hour, 0))
        dead_lettered.append(dead_letter_by_hour.get(hour, 0))

    return {"labels": labels, "completed": completed, "dead_lettered": dead_lettered}
