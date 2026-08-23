"""Job handlers for the watcher pipeline: watch_fetch -> watch_diff ->
watch_notify (registered into the normal handler registry; the scheduler
materializes them as a job DAG per tick, see app/services/watch_service.py).

Fetch etiquette and safety, in order of application:
1. SSRF guard: scheme/port allowlist and all resolved addresses must be
   public (app/services/url_safety.py) -- enforced at fetch time.
2. robots.txt: fetched per origin (cached with a TTL) and honored; a
   disallow raises, which dead-letters the tick and eventually marks the
   watch broken -- retrying a policy block is pointless.
3. Per-domain rate limit: at most one fetch per host per
   DOMAIN_MIN_INTERVAL_SECONDS within this worker process, sleeping out
   the remainder (bounded, watch intervals are >= 60s).
4. Response caps: bounded redirects (each hop re-guarded) and a bounded
   body read.

Error semantics:
- policy violations (SSRF, robots) raise -> retry -> dead-letter ->
  watch marked broken after consecutive tick failures;
- network errors are DATA for kind=down watches (the site being
  unreachable is exactly the condition) and transient failures for the
  other kinds (raise -> normal retry path);
- an HTTP response of any status is a successful check; what it means is
  the diff handler's business.
"""
import asyncio
import hashlib
import logging
import time
import urllib.robotparser
from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import (
    CheckOutcome,
    Watch,
    WatchAlert,
    WatchAlertKind,
    WatchCheck,
    WatchKind,
    WatchState,
)
from app.services.url_safety import UrlPolicyError, ensure_public_url
from app.services.watch_service import create_alert
from worker.handlers import register

logger = logging.getLogger("jobsched.watch")

USER_AGENT = "jobsched-watchbot/1.0 (+https://github.com/Krish231204/Job-Scheduler)"
DOMAIN_MIN_INTERVAL_SECONDS = 10.0
MAX_BODY_BYTES = 512 * 1024
MAX_REDIRECTS = 3
ROBOTS_CACHE_TTL_SECONDS = 3600.0
FETCH_TIMEOUT = httpx.Timeout(15.0, connect=10.0)

_robots_cache: dict[str, tuple[urllib.robotparser.RobotFileParser | None, float]] = {}
_domain_last_fetch: dict[str, float] = {}
_rate_lock = asyncio.Lock()


async def _respect_domain_rate_limit(host: str) -> None:
    """Serialize per-host pacing decisions, then sleep out any remainder.
    In-process only: with multiple worker processes the limit is
    best-effort, which watch intervals >= 60s keep comfortably polite."""
    async with _rate_lock:
        now = time.monotonic()
        last = _domain_last_fetch.get(host, 0.0)
        wait = max(0.0, DOMAIN_MIN_INTERVAL_SECONDS - (now - last))
        _domain_last_fetch[host] = max(now, last + DOMAIN_MIN_INTERVAL_SECONDS) if wait else now
    if wait:
        await asyncio.sleep(wait)


async def _robots_allows(client: httpx.AsyncClient, url: str, scheme: str, host: str) -> bool:
    origin = f"{scheme}://{host}"
    cached = _robots_cache.get(origin)
    if cached is not None and time.monotonic() - cached[1] < ROBOTS_CACHE_TTL_SECONDS:
        parser = cached[0]
    else:
        parser = None
        try:
            resp = await client.get(f"{origin}/robots.txt", headers={"User-Agent": USER_AGENT})
            if resp.status_code == 200:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(resp.text.splitlines())
            # Any non-200 (404, 403, 5xx): treat as no robots policy. The
            # common convention; being stricter would break most targets.
        except httpx.HTTPError:
            parser = None  # robots unreachable -> assume allowed
        _robots_cache[origin] = (parser, time.monotonic())
    if parser is None:
        return True
    return parser.can_fetch(USER_AGENT, url)


async def _read_capped(resp: httpx.Response) -> bytes:
    body = b""
    async for chunk in resp.aiter_bytes():
        body += chunk
        if len(body) >= MAX_BODY_BYTES:
            break
    return body[:MAX_BODY_BYTES]


async def fetch_url_checked(url: str) -> tuple[int, str]:
    """GET a URL with the full safety stack. Returns (status_code, body_text).
    Raises UrlPolicyError on policy blocks, httpx/ConnectionError on
    network-level failures."""
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=False) as client:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            host = await ensure_public_url(current)
            scheme = current.split(":", 1)[0]
            if not await _robots_allows(client, current, scheme, host):
                raise UrlPolicyError(f"policy: {current} is disallowed by robots.txt")
            await _respect_domain_rate_limit(host)

            async with client.stream("GET", current, headers={"User-Agent": USER_AGENT}) as resp:
                if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                    current = str(httpx.URL(current).join(resp.headers["location"]))
                    continue
                body = await _read_capped(resp)
                encoding = resp.charset_encoding or "utf-8"
                return resp.status_code, body.decode(encoding, errors="replace")
        raise UrlPolicyError(f"policy: {url} redirected more than {MAX_REDIRECTS} times")


def _normalized_hash(text: str) -> str:
    """Hash of whitespace-normalized content, so reformatting noise (and
    trailing whitespace churn) doesn't read as a content change."""
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()


@register("watch_fetch")
async def watch_fetch(payload: dict) -> dict:
    watch_id = payload["watch_id"]
    fetch_job_id = payload.get("fetch_job_id")

    async with AsyncSessionLocal() as db:
        watch = await db.get(Watch, watch_id)
        if watch is None or not watch.is_active:
            return {"skipped": "watch missing or inactive"}
        url, kind, keyword = watch.url, watch.kind, watch.keyword

    started = datetime.now(timezone.utc)
    t0 = time.monotonic()
    http_status: int | None = None
    body: str | None = None
    detail: str | None = None

    try:
        http_status, body = await fetch_url_checked(url)
    except UrlPolicyError:
        raise  # dead-letters the tick; the watch goes broken after repeats
    except (httpx.HTTPError, ConnectionError, OSError) as exc:
        if kind != WatchKind.DOWN:
            # Transient for keyword/content watches -- let the retry
            # policy have it; repeated dead-letters mark the watch broken.
            raise
        detail = f"unreachable: {exc.__class__.__name__}: {exc}" [:500]

    latency_ms = int((time.monotonic() - t0) * 1000)

    async with AsyncSessionLocal() as db:
        check = WatchCheck(
            watch_id=watch_id,
            fetch_job_id=fetch_job_id,
            started_at=started,
            latency_ms=latency_ms,
            http_status=http_status,
            content_hash=_normalized_hash(body) if body is not None else None,
            keyword_found=(keyword.lower() in body.lower()) if (kind == WatchKind.KEYWORD and keyword and body is not None) else None,
            detail=detail,
        )
        db.add(check)
        await db.commit()
        return {"check_id": check.id, "http_status": http_status, "latency_ms": latency_ms}


@register("watch_diff")
async def watch_diff(payload: dict) -> dict:
    watch_id = payload["watch_id"]
    fetch_job_id = payload["fetch_job_id"]

    async with AsyncSessionLocal() as db:
        watch = await db.get(Watch, watch_id)
        if watch is None:
            return {"skipped": "watch missing"}
        check = (
            await db.execute(select(WatchCheck).where(WatchCheck.fetch_job_id == fetch_job_id))
        ).scalar_one_or_none()
        if check is None:
            raise RuntimeError(f"no check recorded for fetch job {fetch_job_id}")

        if watch.kind == WatchKind.DOWN:
            triggered = check.http_status is None or check.http_status >= 400
            reason = (
                f"{watch.url} is unreachable ({check.detail})" if check.http_status is None
                else f"{watch.url} returned HTTP {check.http_status}"
            ) if triggered else f"{watch.url} is up (HTTP {check.http_status})"
        elif watch.kind == WatchKind.KEYWORD:
            triggered = bool(check.keyword_found)
            reason = (
                f"keyword {watch.keyword!r} found on {watch.url}" if triggered
                else f"keyword {watch.keyword!r} not present on {watch.url}"
            )
        else:  # CONTENT_CHANGE
            previous = (
                await db.execute(
                    select(WatchCheck)
                    .where(
                        WatchCheck.watch_id == watch_id,
                        WatchCheck.id != check.id,
                        WatchCheck.content_hash.isnot(None),
                    )
                    .order_by(WatchCheck.started_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            triggered = (
                previous is not None
                and check.content_hash is not None
                and previous.content_hash != check.content_hash
            )
            reason = f"content of {watch.url} changed" if triggered else f"content of {watch.url} unchanged"

        new_state = WatchState.TRIGGERED if triggered else WatchState.OK
        old_state = watch.state

        if new_state == WatchState.TRIGGERED and old_state != WatchState.TRIGGERED:
            await create_alert(
                db, watch,
                check_id=check.id,
                kind=WatchAlertKind.TRIGGERED,
                message=f"Watch '{watch.name}': {reason}",
                dedupe_key=f"watch:{watch_id}:triggered:{check.id}",
            )
        elif new_state == WatchState.OK and old_state == WatchState.TRIGGERED:
            await create_alert(
                db, watch,
                check_id=check.id,
                kind=WatchAlertKind.RECOVERED,
                message=f"Watch '{watch.name}' recovered: {reason}",
                dedupe_key=f"watch:{watch_id}:recovered:{check.id}",
            )

        check.outcome = CheckOutcome.TRIGGERED if triggered else CheckOutcome.OK
        watch.state = new_state
        watch.consecutive_failures = 0  # the check pipeline itself worked
        await db.commit()
        return {"outcome": check.outcome.value, "state": new_state.value}


@register("watch_notify")
async def watch_notify(payload: dict) -> dict:
    watch_id = payload["watch_id"]

    async with AsyncSessionLocal() as db:
        watch = await db.get(Watch, watch_id)
        if watch is None:
            return {"skipped": "watch missing"}
        alerts = list(
            (
                await db.execute(
                    select(WatchAlert)
                    .where(WatchAlert.watch_id == watch_id, WatchAlert.delivered.is_(False))
                    .order_by(WatchAlert.created_at)
                )
            ).scalars().all()
        )
        webhook_url = watch.webhook_url
        watch_name = watch.name
        alert_data = [(a.id, a.kind.value, a.message, a.created_at.isoformat()) for a in alerts]

    if not alert_data:
        return {"delivered": 0}

    delivered = 0
    for alert_id, kind, message, created_at in alert_data:
        delivery_detail = "dashboard only (no webhook configured)"
        if webhook_url:
            # Webhook targets get the same SSRF guard as watch targets. A
            # failed delivery raises: the tick retries, and because
            # `delivered` only flips after success, the next tick's notify
            # picks unsent alerts back up (idempotent catch-up).
            await ensure_public_url(webhook_url)
            async with httpx.AsyncClient(timeout=FETCH_TIMEOUT) as client:
                resp = await client.post(
                    webhook_url,
                    headers={"User-Agent": USER_AGENT},
                    json={
                        "watch_id": watch_id,
                        "watch": watch_name,
                        "alert": kind,
                        "message": message,
                        "at": created_at,
                    },
                )
                if resp.status_code >= 400:
                    raise RuntimeError(f"webhook returned HTTP {resp.status_code}")
            delivery_detail = f"webhook {resp.status_code}"

        async with AsyncSessionLocal() as db:
            alert = await db.get(WatchAlert, alert_id)
            if alert is not None and not alert.delivered:
                alert.delivered = True
                alert.delivery_detail = delivery_detail
                await db.commit()
                delivered += 1

    return {"delivered": delivered}
