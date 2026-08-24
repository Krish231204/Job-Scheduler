"""Watcher tests: queue provisioning, DAG materialization, broken-watch
detection, idempotent alerting, condition evaluation (diff), delivery
bookkeeping (notify), and the URL safety guard.

Handler tests point worker.watch_handlers at the test database by
monkeypatching its AsyncSessionLocal -- the handlers deliberately open
their own sessions (they run in the worker process), so this is the
seam. Fetch itself (HTTP, robots, rate limiting) is exercised end-to-end
by scripts/watch_soak.py against a local server rather than unit-mocked.
"""
from datetime import datetime, timedelta, timezone

import time

import pytest
from sqlalchemy import select

import worker.watch_handlers as wh
from app.models import (
    Job,
    JobDependency,
    JobStatus,
    Organization,
    Project,
    Watch,
    WatchAlert,
    WatchAlertKind,
    WatchCheck,
    WatchKind,
    WatchState,
)
from app.services.url_safety import UrlPolicyError, ensure_public_url, validate_url_syntax
from app.services.watch_service import (
    BROKEN_AFTER_CONSECUTIVE_FAILURES,
    create_alert,
    get_or_create_watch_queue,
    materialize_due_watches,
)
from tests.conftest import requires_db


async def _make_org(db) -> Organization:
    org = Organization(name="Watch Org")
    db.add(org)
    await db.flush()
    return org


async def _make_watch(db, org, queue, **overrides) -> Watch:
    fields = dict(
        organization_id=org.id,
        queue_id=queue.id,
        name="test watch",
        url="https://example.com/",
        kind=WatchKind.DOWN,
        interval_seconds=60,
        next_check_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    fields.update(overrides)
    watch = Watch(**fields)
    db.add(watch)
    await db.flush()
    return watch


@requires_db
async def test_watch_queue_provisioned_once_per_org(db_session):
    org = await _make_org(db_session)
    queue1 = await get_or_create_watch_queue(db_session, org.id)
    queue2 = await get_or_create_watch_queue(db_session, org.id)
    assert queue1.id == queue2.id

    project = await db_session.get(Project, queue1.project_id)
    assert project.name == "Watches"
    assert project.organization_id == org.id


@requires_db
async def test_materialize_creates_fetch_diff_notify_dag(db_session):
    org = await _make_org(db_session)
    queue = await get_or_create_watch_queue(db_session, org.id)
    watch = await _make_watch(db_session, org, queue)

    materialized = await materialize_due_watches(db_session)
    assert materialized == 1

    jobs = (
        await db_session.execute(select(Job).where(Job.queue_id == queue.id).order_by(Job.id))
    ).scalars().all()
    assert [j.name for j in jobs] == ["watch_fetch", "watch_diff", "watch_notify"]
    fetch, diff, notify = jobs
    assert fetch.status == JobStatus.QUEUED
    assert diff.status == JobStatus.BLOCKED
    assert notify.status == JobStatus.BLOCKED
    assert fetch.payload == {"watch_id": watch.id, "fetch_job_id": fetch.id}
    assert fetch.timeout_seconds == 30.0

    edges = (
        await db_session.execute(select(JobDependency).order_by(JobDependency.job_id))
    ).scalars().all()
    assert [(e.job_id, e.depends_on_job_id) for e in edges] == [(diff.id, fetch.id), (notify.id, diff.id)]

    assert watch.last_fetch_job_id == fetch.id
    assert watch.next_check_at > datetime.now(timezone.utc)

    # Not due any more: a second pass materializes nothing.
    assert await materialize_due_watches(db_session) == 0


@requires_db
async def test_watch_goes_broken_after_consecutive_dead_letters(db_session):
    org = await _make_org(db_session)
    queue = await get_or_create_watch_queue(db_session, org.id)
    watch = await _make_watch(db_session, org, queue, consecutive_failures=BROKEN_AFTER_CONSECUTIVE_FAILURES - 1)

    dead_job = Job(queue_id=queue.id, name="watch_fetch", job_type="immediate", status=JobStatus.DEAD_LETTER, payload={})
    db_session.add(dead_job)
    await db_session.flush()
    watch.last_fetch_job_id = dead_job.id
    await db_session.flush()

    assert await materialize_due_watches(db_session) == 0  # broken, not materialized
    assert watch.state == WatchState.BROKEN
    assert watch.is_active is False

    alerts = (await db_session.execute(select(WatchAlert).where(WatchAlert.watch_id == watch.id))).scalars().all()
    assert len(alerts) == 1
    assert alerts[0].kind == WatchAlertKind.BROKEN


@requires_db
async def test_create_alert_is_idempotent(db_session):
    org = await _make_org(db_session)
    queue = await get_or_create_watch_queue(db_session, org.id)
    watch = await _make_watch(db_session, org, queue)

    first = await create_alert(db_session, watch, check_id=None, kind=WatchAlertKind.TRIGGERED, message="m", dedupe_key="k1")
    second = await create_alert(db_session, watch, check_id=None, kind=WatchAlertKind.TRIGGERED, message="m", dedupe_key="k1")
    assert first is not None and second is None

    count = len((await db_session.execute(select(WatchAlert).where(WatchAlert.watch_id == watch.id))).scalars().all())
    assert count == 1


@pytest.fixture
def handler_db(session_factory, monkeypatch):
    """Point the watch handlers' own sessions at the test database."""
    monkeypatch.setattr(wh, "AsyncSessionLocal", session_factory)
    return session_factory


async def _committed_watch(session_factory, kind, **overrides):
    async with session_factory() as db:
        org = await _make_org(db)
        queue = await get_or_create_watch_queue(db, org.id)
        watch = await _make_watch(db, org, queue, kind=kind, **overrides)
        await db.commit()
        return watch.id, queue.id


async def _committed_check(session_factory, watch_id, queue_id, **fields):
    """Create a (completed) fetch Job + its WatchCheck, returning the job id
    that the diff handler uses to find the tick's check."""
    async with session_factory() as db:
        job = Job(queue_id=queue_id, name="watch_fetch", job_type="immediate", status=JobStatus.COMPLETED, payload={})
        db.add(job)
        await db.flush()
        check = WatchCheck(watch_id=watch_id, fetch_job_id=job.id, **fields)
        db.add(check)
        await db.commit()
        return job.id


@requires_db
async def test_diff_down_transitions_and_alerts_once(handler_db):
    watch_id, queue_id = await _committed_watch(handler_db, WatchKind.DOWN)

    fj = await _committed_check(handler_db, watch_id, queue_id, http_status=None, detail="unreachable: ConnectError")
    result = await wh.watch_diff({"watch_id": watch_id, "fetch_job_id": fj})
    assert result == {"outcome": "triggered", "state": "triggered"}

    # Still down next tick: no second alert (transition-based alerting).
    fj = await _committed_check(handler_db, watch_id, queue_id, http_status=503)
    await wh.watch_diff({"watch_id": watch_id, "fetch_job_id": fj})

    # Recovery produces exactly one 'recovered' alert.
    fj = await _committed_check(handler_db, watch_id, queue_id, http_status=200)
    result = await wh.watch_diff({"watch_id": watch_id, "fetch_job_id": fj})
    assert result["state"] == "ok"

    async with handler_db() as db:
        alerts = (
            await db.execute(select(WatchAlert).where(WatchAlert.watch_id == watch_id).order_by(WatchAlert.id))
        ).scalars().all()
        assert [a.kind for a in alerts] == [WatchAlertKind.TRIGGERED, WatchAlertKind.RECOVERED]


@requires_db
async def test_diff_keyword_condition(handler_db):
    watch_id, queue_id = await _committed_watch(handler_db, WatchKind.KEYWORD, keyword="sold out")

    fj = await _committed_check(handler_db, watch_id, queue_id, http_status=200, keyword_found=False)
    result = await wh.watch_diff({"watch_id": watch_id, "fetch_job_id": fj})
    assert result["state"] == "ok"

    fj = await _committed_check(handler_db, watch_id, queue_id, http_status=200, keyword_found=True)
    result = await wh.watch_diff({"watch_id": watch_id, "fetch_job_id": fj})
    assert result["state"] == "triggered"


@requires_db
async def test_diff_content_change_uses_previous_hash(handler_db):
    watch_id, queue_id = await _committed_watch(handler_db, WatchKind.CONTENT_CHANGE)

    fj = await _committed_check(handler_db, watch_id, queue_id, http_status=200, content_hash="aaa")
    result = await wh.watch_diff({"watch_id": watch_id, "fetch_job_id": fj})
    assert result["state"] == "ok"  # first check is the baseline

    fj = await _committed_check(handler_db, watch_id, queue_id, http_status=200, content_hash="aaa")
    result = await wh.watch_diff({"watch_id": watch_id, "fetch_job_id": fj})
    assert result["state"] == "ok"

    fj = await _committed_check(handler_db, watch_id, queue_id, http_status=200, content_hash="bbb")
    result = await wh.watch_diff({"watch_id": watch_id, "fetch_job_id": fj})
    assert result["state"] == "triggered"


@requires_db
async def test_notify_marks_alerts_delivered_without_webhook(handler_db):
    watch_id, queue_id = await _committed_watch(handler_db, WatchKind.DOWN)
    fj = await _committed_check(handler_db, watch_id, queue_id, http_status=500)
    await wh.watch_diff({"watch_id": watch_id, "fetch_job_id": fj})

    result = await wh.watch_notify({"watch_id": watch_id, "diff_job_id": 0})
    assert result == {"delivered": 1}

    async with handler_db() as db:
        alert = (await db.execute(select(WatchAlert).where(WatchAlert.watch_id == watch_id))).scalar_one()
        assert alert.delivered is True

    # Idempotent: nothing left to deliver.
    result = await wh.watch_notify({"watch_id": watch_id, "diff_job_id": 0})
    assert result == {"delivered": 0}


# ------------------------------------------------------------------
# URL safety (pure / resolution-based, no app DB)
# ------------------------------------------------------------------

def test_url_syntax_guard():
    assert validate_url_syntax("https://example.com/page") == "example.com"
    for bad in [
        "ftp://example.com/",              # scheme
        "https://example.com:2222/",       # port
        "https://user:pw@example.com/",    # credentials
        "http://169.254.169.254/latest/",  # EC2 metadata service
        "http://127.0.0.1/",               # loopback literal
        "http://10.0.0.8/",                # private literal
    ]:
        with pytest.raises(UrlPolicyError):
            validate_url_syntax(bad)


async def test_resolution_guard_blocks_localhost():
    with pytest.raises(UrlPolicyError):
        await ensure_public_url("http://localhost/")


# ------------------------------------------------------------------
# Fetch etiquette
# ------------------------------------------------------------------

async def test_redirect_chain_pays_domain_rate_limit_once(monkeypatch):
    """Found in production: a same-host redirect made every check sleep out
    the full per-domain interval (the limiter treated hop 2 as a new fetch),
    so p50 latency read as min_interval + real RTT. A chain must be charged
    once per host."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", "/end")
                self.end_headers()
            else:
                body = b"arrived"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, *args):  # keep test output quiet
            pass

    # The port allowlist applies even with private targets allowed, so the
    # server must sit on an allowed port.
    server = port = None
    for candidate in (8080, 8000, 8443):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
            port = candidate
            break
        except OSError:
            continue
    if server is None:
        pytest.skip("no allowed port free for local test server")
    threading.Thread(target=server.serve_forever, daemon=True).start()

    settings = wh.get_settings()
    monkeypatch.setattr(settings, "watch_allow_private_targets", True)
    monkeypatch.setattr(settings, "watch_domain_min_interval_seconds", 30.0)
    # This process may have fetched from 127.0.0.1 before (other tests /
    # reused workers); a stale timestamp would make even the first hop wait.
    monkeypatch.setattr(wh, "_domain_last_fetch", {})

    try:
        t0 = time.monotonic()
        status, text = await wh.fetch_url_checked(f"http://127.0.0.1:{port}/start")
        elapsed = time.monotonic() - t0
    finally:
        server.shutdown()

    assert (status, text) == (200, "arrived")
    assert elapsed < 5.0, f"redirect hop was rate-limited: fetch took {elapsed:.1f}s"
