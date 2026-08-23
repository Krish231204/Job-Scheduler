"""Watcher soak benchmark: run the real watcher pipeline (scheduler
materialization -> fetch/diff/notify DAG -> real WorkerRunner execution)
against a deterministic local HTTP target and report checks executed and
per-check latency percentiles.

Deterministic by construction: the target server is local and fixed, no
randomness anywhere, and the tick cadence is accelerated (each watch is
re-armed as soon as its previous tick's DAG finishes) so a short run
produces a statistically useful number of checks. Acceleration changes
how often checks happen, not what each check costs -- the latency
percentiles are honest per-check numbers; the checks/sec figure is a
throughput ceiling for this pool shape, not a claim about 5-minute
intervals.

Usage:

    python -m scripts.watch_soak                     # 12 watches, 90s, 1 worker x 8
    python -m scripts.watch_soak --watches 20 --duration 120 --workers 2

Runs against a dedicated database (WATCH_SOAK_DATABASE_URL, default the
jobsched_bench DB) which is truncated at start. The local target listens
on 127.0.0.1:8080, so the run sets WATCH_ALLOW_PRIVATE_TARGETS -- the
SSRF guard's dev escape hatch -- for this process only.
"""
import argparse
import asyncio
import http.server
import json
import os
import platform
import threading
import time


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watcher soak benchmark")
    parser.add_argument("--watches", type=int, default=12, help="Watches to register (default 12)")
    parser.add_argument("--duration", type=int, default=90, help="Soak duration in seconds (default 90)")
    parser.add_argument("--workers", type=int, default=1, help="WorkerRunner instances (default 1)")
    parser.add_argument("--concurrency", type=int, default=8, help="Per-worker concurrency (default 8)")
    parser.add_argument("--port", type=int, default=8080, help="Local target server port (default 8080)")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    return parser.parse_args()


ARGS = _parse_args()

# Environment must be set before any `app.` import (settings are cached).
os.environ["DATABASE_URL"] = os.environ.get(
    "WATCH_SOAK_DATABASE_URL", "postgresql+asyncpg://jobsched:jobsched@localhost:5432/jobsched_bench"
)
os.environ["WATCH_ALLOW_PRIVATE_TARGETS"] = "true"
os.environ["WATCH_DOMAIN_MIN_INTERVAL_SECONDS"] = "0"
os.environ["WORKER_POLL_INTERVAL_SECONDS"] = "0.2"

import logging  # noqa: E402

logging.getLogger("jobsched.worker").setLevel(logging.WARNING)
logging.getLogger("jobsched.watch").setLevel(logging.WARNING)

from sqlalchemy import create_engine, func, select, text, update  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.database import AsyncSessionLocal, Base  # noqa: E402
from app.models import (  # noqa: E402
    CheckOutcome,
    Organization,
    Watch,
    WatchAlert,
    WatchCheck,
    WatchKind,
)
from app.services.watch_service import get_or_create_watch_queue, materialize_due_watches  # noqa: E402
from worker.runner import WorkerRunner  # noqa: E402


# Not imported from scripts.benchmark: that module parses its own argv at
# import time, which would clash with this script's arguments.
def _ensure_database_exists(url: str) -> None:
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
    # Drop + recreate rather than truncate: the soak DB may hold a stale
    # schema from an older code revision, and create_all skips existing
    # tables.
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()

# The three target behaviors, all deterministic. /flip alternates its body
# every request, so content_change watches exercise real transitions.
_PAGES = {
    "/ok": (200, "service is healthy and running fine"),
    "/down": (503, "service unavailable"),
    "/page": (200, "product page -- currently sold out until further notice"),
}
_flip_counter = {"n": 0}


class _Target(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib API
        if self.path == "/robots.txt":
            body = "User-agent: *\nAllow: /\n"
            status = 200
        elif self.path == "/flip":
            _flip_counter["n"] += 1
            body = f"revision block {'A' if (_flip_counter['n'] // 3) % 2 == 0 else 'B'}"
            status = 200
        else:
            status, body = _PAGES.get(self.path, (404, "not found"))
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # silence per-request stderr noise
        pass


def _start_target(port: int) -> http.server.ThreadingHTTPServer:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Target)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


async def _seed_watches(n: int, port: int) -> int:
    """One org, one watch queue, n watches spread across the four target
    behaviors. Returns the org id."""
    kinds = [
        (WatchKind.DOWN, "/ok", None),
        (WatchKind.DOWN, "/down", None),
        (WatchKind.KEYWORD, "/page", "sold out"),
        (WatchKind.CONTENT_CHANGE, "/flip", None),
    ]
    async with AsyncSessionLocal() as db:
        org = Organization(name="soak-org")
        db.add(org)
        await db.flush()
        queue = await get_or_create_watch_queue(db, org.id)
        for i in range(n):
            kind, path, keyword = kinds[i % len(kinds)]
            db.add(Watch(
                organization_id=org.id,
                queue_id=queue.id,
                name=f"soak-{i:02d}-{kind.value}",
                url=f"http://127.0.0.1:{port}{path}",
                kind=kind,
                keyword=keyword,
                interval_seconds=60,
                next_check_at=func.now(),
            ))
        await db.commit()
        return org.id


async def _scheduler_loop(stop: asyncio.Event) -> None:
    """In-process stand-in for scheduler/main.py's loop, with acceleration:
    every pass re-arms all active watches so a new tick starts as soon as
    the previous tick's DAG has finished (the materializer skips watches
    whose last fetch is still in flight)."""
    while not stop.is_set():
        async with AsyncSessionLocal() as db:
            await db.execute(update(Watch).where(Watch.is_active.is_(True)).values(next_check_at=func.now()))
            await materialize_due_watches(db)
            await db.commit()
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.5)
        except TimeoutError:
            pass


async def main() -> None:
    url = os.environ["DATABASE_URL"]
    _ensure_database_exists(url)
    await _reset_schema(url)

    target = _start_target(ARGS.port)
    await _seed_watches(ARGS.watches, ARGS.port)

    stop = asyncio.Event()
    runners = [WorkerRunner(concurrency=ARGS.concurrency) for _ in range(ARGS.workers)]
    tasks = [asyncio.create_task(r.run()) for r in runners]
    tasks.append(asyncio.create_task(_scheduler_loop(stop)))

    started = time.perf_counter()
    await asyncio.sleep(ARGS.duration)
    stop.set()
    for r in runners:
        r.request_shutdown()
    await asyncio.gather(*tasks, return_exceptions=True)
    elapsed = time.perf_counter() - started
    target.shutdown()

    async with AsyncSessionLocal() as db:
        total_checks = (await db.execute(select(func.count(WatchCheck.id)))).scalar_one()
        outcomes = dict(
            (await db.execute(select(WatchCheck.outcome, func.count(WatchCheck.id)).group_by(WatchCheck.outcome))).all()
        )
        p50, p95, p99 = (
            await db.execute(
                select(
                    func.percentile_cont(0.5).within_group(WatchCheck.latency_ms),
                    func.percentile_cont(0.95).within_group(WatchCheck.latency_ms),
                    func.percentile_cont(0.99).within_group(WatchCheck.latency_ms),
                ).where(WatchCheck.latency_ms.isnot(None))
            )
        ).one()
        alerts = (await db.execute(select(func.count(WatchAlert.id)))).scalar_one()
        jobs_done = (await db.execute(text("SELECT count(*) FROM jobs WHERE status = 'completed'"))).scalar_one()

    results = {
        "config": {
            "watches": ARGS.watches,
            "duration_seconds": ARGS.duration,
            "workers": ARGS.workers,
            "concurrency_per_worker": ARGS.concurrency,
            "python": platform.python_version(),
            "cpus": os.cpu_count(),
        },
        "checks_executed": total_checks,
        "checks_per_second": round(total_checks / elapsed, 2),
        "outcomes": {(k.value if isinstance(k, CheckOutcome) else str(k)): v for k, v in outcomes.items()},
        "alerts_created": alerts,
        "pipeline_jobs_completed": jobs_done,
        "check_latency_ms": {
            "p50": round(float(p50), 1) if p50 is not None else None,
            "p95": round(float(p95), 1) if p95 is not None else None,
            "p99": round(float(p99), 1) if p99 is not None else None,
        },
    }

    if ARGS.json:
        print(json.dumps(results, indent=2))
        return

    lat = results["check_latency_ms"]
    print(f"""
Watcher soak  (Python {results['config']['python']}, {results['config']['cpus']} CPUs)
=====================================================
Pool        {ARGS.workers} worker(s) x concurrency {ARGS.concurrency}, {ARGS.watches} watches, {ARGS.duration}s accelerated ticks
Checks      {total_checks} executed  ({results['checks_per_second']}/sec sustained)
Outcomes    {results['outcomes']}
Alerts      {alerts} (transition-based, idempotent)
DAG jobs    {jobs_done} completed (fetch + diff + notify)

Check latency (ms)   p50 {lat['p50']}   p95 {lat['p95']}   p99 {lat['p99']}
(latency = full guarded fetch: SSRF resolve + robots + HTTP round trip)
""")


if __name__ == "__main__":
    asyncio.run(main())
