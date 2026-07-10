"""Scheduler process: a single lightweight loop responsible for time-based
transitions that don't belong on the hot worker-poll path:

1. Materializing due ScheduledJob definitions (cron + one-off) into concrete
   Job rows.
2. Promoting RETRYING jobs back to QUEUED once their backoff delay elapses.
3. Detecting workers whose heartbeat has gone stale (crashed / network
   partition) and requeuing whatever they had claimed, so jobs are never
   silently stuck.

Only one scheduler instance should run per cluster (or run several behind a
distributed lock -- see docs/DESIGN_DECISIONS.md) since duplicate scheduler
instances would just do redundant, harmless work here (all mutations are
idempotent/guarded by SKIP LOCKED), but running many wastes DB round trips.
"""
import asyncio
import logging
import signal

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.services.job_service import detect_stale_workers, materialize_due_scheduled_jobs, promote_retrying_jobs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger("codity.scheduler")
settings = get_settings()

_shutdown = asyncio.Event()


async def tick() -> None:
    async with AsyncSessionLocal() as db:
        materialized = await materialize_due_scheduled_jobs(db)
        promoted = await promote_retrying_jobs(db)
        requeued = await detect_stale_workers(db, settings.worker_heartbeat_timeout_seconds)
        await db.commit()
        if materialized or promoted or requeued:
            logger.info(
                "tick: materialized=%s promoted_retries=%s requeued_from_stale_workers=%s",
                materialized, promoted, requeued,
            )


async def main() -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _shutdown.set)

    logger.info("Scheduler started (poll interval=%.1fs)", settings.scheduler_poll_interval_seconds)
    while not _shutdown.is_set():
        try:
            await tick()
        except Exception:  # noqa: BLE001 - keep the loop alive across transient DB errors
            logger.exception("Scheduler tick failed")
        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=settings.scheduler_poll_interval_seconds)
        except asyncio.TimeoutError:
            pass
    logger.info("Scheduler shut down")


if __name__ == "__main__":
    asyncio.run(main())
