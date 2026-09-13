import asyncio
import contextlib
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.timefmt import display_tz, format_ts
from app.database import get_db
from app.deps import get_job_for_user, get_project_for_user, get_queue_for_user, get_queue_role, get_watch_for_user
from app.models import (
    Job,
    JobStatus,
    Organization,
    OrganizationMember,
    OrgRole,
    Project,
    Queue,
    ScheduledJob,
    User,
    Watch,
    WatchAlert,
    WatchCheck,
    Worker,
)
from app.rate_limit import limiter
from app.security import create_access_token, decode_token, hash_password, verify_password
from app.services.stats import queue_health_series, queue_stats, watch_latency_series, watch_stats
from app.web_auth import COOKIE_NAME, get_current_user_from_cookie, require_web_user

WS_UPDATE_INTERVAL_SECONDS = 2.0

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory="templates")
settings = get_settings()


def _format_ts(value: datetime | None) -> str:
    """Compact, second-precision timestamp for the dashboard's data voice
    (raw datetime repr drags microseconds + offset into every table cell).
    Rendered in ``settings.dashboard_timezone`` (default Asia/Kolkata);
    storage stays UTC. See app/timefmt.py."""
    return format_ts(value)


templates.env.filters["ts"] = _format_ts
templates.env.globals["display_tz"] = str(display_tz())


def _set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=settings.effective_cookie_secure,
        max_age=60 * 60 * 24,
    )


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
@limiter.limit("5/minute")
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(password, user.hashed_password):
        return templates.TemplateResponse(request, "login.html", {"error": "Invalid email or password"}, status_code=401)
    token = create_access_token(subject=str(user.id))
    response = RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    _set_session_cookie(response, token)
    return response


@router.get("/register", response_class=HTMLResponse)
async def register_form(request: Request):
    return templates.TemplateResponse(request, "register.html", {"error": None})


@router.post("/register")
async def register_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    full_name: str = Form(""),
    org_name: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    existing = await db.execute(select(User).where(User.email == email))
    if existing.scalar_one_or_none() is not None:
        return templates.TemplateResponse(request, "register.html", {"error": "Email already registered"}, status_code=409)

    user = User(email=email, hashed_password=hash_password(password), full_name=full_name)
    db.add(user)
    await db.flush()

    org = Organization(name=org_name)
    db.add(org)
    await db.flush()
    db.add(OrganizationMember(organization_id=org.id, user_id=user.id, role=OrgRole.OWNER))

    project = Project(organization_id=org.id, name="Getting Started", created_by=user.id)
    db.add(project)
    await db.flush()

    queue = Queue(project_id=project.id, name="general", priority=0, max_concurrency=4)
    db.add(queue)
    await db.flush()
    from app.models import RetryPolicy
    db.add(RetryPolicy(queue_id=queue.id))

    await db.commit()

    token = create_access_token(subject=str(user.id))
    response = RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    _set_session_cookie(response, token)
    return response


@router.post("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(COOKIE_NAME)
    return response


@router.get("/", response_class=HTMLResponse)
async def root(request: Request, user: User | None = Depends(get_current_user_from_cookie)):
    if user is None:
        return RedirectResponse(url="/login")
    return RedirectResponse(url="/dashboard")


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_home(request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(require_web_user)):
    result = await db.execute(
        select(Project, Organization)
        .join(Organization, Organization.id == Project.organization_id)
        .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .where(OrganizationMember.user_id == user.id)
    )
    projects = [{"project": p, "organization": o} for p, o in result.all()]

    worker_result = await db.execute(select(Worker).order_by(Worker.last_seen_at.desc()))
    workers = list(worker_result.scalars().all())
    online_cutoff = datetime.now(timezone.utc) - timedelta(seconds=30)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"user": user, "projects": projects, "workers": workers, "online_cutoff": online_cutoff},
    )


@router.get("/dashboard/projects/{project_id}", response_class=HTMLResponse)
async def dashboard_project(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_web_user),
    project: Project = Depends(get_project_for_user),
):
    result = await db.execute(
        select(Queue).options(selectinload(Queue.retry_policy)).where(Queue.project_id == project.id)
    )
    queues = list(result.scalars().all())
    stats_by_queue = {q.id: await queue_stats(db, q.id) for q in queues}

    return templates.TemplateResponse(
        request,
        "project.html",
        {"user": user, "project": project, "queues": queues, "stats": stats_by_queue},
    )


@router.get("/dashboard/queues/{queue_id}", response_class=HTMLResponse)
async def dashboard_queue(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_web_user),
    queue: Queue = Depends(get_queue_for_user),
    status_filter: str | None = None,
):
    filters = [Job.queue_id == queue.id]
    if status_filter:
        filters.append(Job.status == JobStatus(status_filter))

    jobs_result = await db.execute(select(Job).where(*filters).order_by(Job.created_at.desc()).limit(100))
    jobs = list(jobs_result.scalars().all())
    stats = await queue_stats(db, queue.id)
    health_series = await queue_health_series(db, queue.id, hours=24)

    sj_result = await db.execute(select(ScheduledJob).where(ScheduledJob.queue_id == queue.id))
    scheduled_jobs = list(sj_result.scalars().all())

    role = await get_queue_role(db, queue.id, user.id)
    is_queue_admin = role in (OrgRole.OWNER, OrgRole.ADMIN)

    return templates.TemplateResponse(
        request,
        "queue_detail.html",
        {
            "user": user,
            "queue": queue,
            "jobs": jobs,
            "stats": stats,
            "health_series": health_series,
            "scheduled_jobs": scheduled_jobs,
            "statuses": list(JobStatus),
            "status_filter": status_filter,
            "is_queue_admin": is_queue_admin,
        },
    )


async def _ws_live_loop(websocket: WebSocket, db: AsyncSession, render_tick) -> None:
    """Shared loop behind every /ws/* live-update endpoint: authenticate
    from the session cookie, then push a freshly-rendered HTML fragment to
    the browser every couple of seconds, replacing the old
    <meta http-equiv="refresh"> full-page reloads. Renders server-side and
    ships HTML, not JSON, so there's one rendering implementation (Jinja2)
    instead of duplicating row/badge markup in JS -- consistent with this
    dashboard's thin-JS-islands approach (see docs/DESIGN_DECISIONS.md).

    `render_tick(db, user_id)` returns the rendered fragment, or None when
    the resource doesn't exist / isn't visible to this user (closes 4404,
    indistinguishable from not-found by design, same as the HTTP routes).

    Takes `db` via Depends(get_db) at the route rather than opening
    AsyncSessionLocal() directly -- the same one session lives for the
    whole connection (committing after each tick's read releases the
    pooled connection between polls rather than holding it the whole
    time), and, just as importantly, this is what makes
    app.dependency_overrides[get_db] work in tests the same way it does
    for every HTTP route.

    WebSocket connections can't set custom headers from browser JS, so
    auth comes from the same-origin session cookie (sent automatically on
    the handshake), decoded the same way get_current_user_from_cookie
    does for HTTP requests -- this can't reuse that dependency directly
    since it's typed against Request, not WebSocket.
    """
    token = websocket.cookies.get(COOKIE_NAME)
    if not token:
        await websocket.close(code=4401)
        return
    try:
        user_id = int(decode_token(token))
    except Exception:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    try:
        while True:
            html = await render_tick(db, user_id)
            await db.commit()  # release the pooled connection between polls
            if html is None:
                await websocket.close(code=4404)
                return
            await websocket.send_text(html)
            await asyncio.sleep(WS_UPDATE_INTERVAL_SECONDS)
    except WebSocketDisconnect:
        pass
    finally:
        with contextlib.suppress(Exception):
            await websocket.close()


@router.websocket("/ws/queues/{queue_id}")
async def ws_queue_updates(
    websocket: WebSocket,
    queue_id: int,
    db: AsyncSession = Depends(get_db),
    status_filter: str | None = None,
):
    """Live stats + job explorer for the queue detail page."""
    if status_filter:
        try:
            JobStatus(status_filter)
        except ValueError:
            await websocket.close(code=4400)
            return

    async def render_tick(db: AsyncSession, user_id: int) -> str | None:
        result = await db.execute(
            select(Queue)
            .options(selectinload(Queue.retry_policy))
            .join(Project, Project.id == Queue.project_id)
            .join(Organization, Organization.id == Project.organization_id)
            .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
            .where(Queue.id == queue_id, OrganizationMember.user_id == user_id)
        )
        queue = result.scalar_one_or_none()
        if queue is None:
            return None

        filters = [Job.queue_id == queue.id]
        if status_filter:
            filters.append(Job.status == JobStatus(status_filter))
        jobs_result = await db.execute(
            select(Job).where(*filters).order_by(Job.created_at.desc()).limit(100)
        )
        jobs = list(jobs_result.scalars().all())
        stats = await queue_stats(db, queue.id)

        return templates.get_template("_queue_live_fragment.html").render(
            queue=queue,
            jobs=jobs,
            stats=stats,
            statuses=list(JobStatus),
            status_filter=status_filter,
        )

    await _ws_live_loop(websocket, db, render_tick)


@router.websocket("/ws/workers")
async def ws_worker_updates(websocket: WebSocket, db: AsyncSession = Depends(get_db)):
    """Live worker table, shared by the dashboard home and workers pages."""

    async def render_tick(db: AsyncSession, user_id: int) -> str:
        result = await db.execute(select(Worker).order_by(Worker.last_seen_at.desc()))
        workers = list(result.scalars().all())
        online_cutoff = datetime.now(timezone.utc) - timedelta(seconds=30)
        return templates.get_template("_workers_live_fragment.html").render(
            workers=workers, online_cutoff=online_cutoff
        )

    await _ws_live_loop(websocket, db, render_tick)


@router.websocket("/ws/projects/{project_id}")
async def ws_project_updates(websocket: WebSocket, project_id: int, db: AsyncSession = Depends(get_db)):
    """Live queue table (per-queue stats) for the project page."""

    async def render_tick(db: AsyncSession, user_id: int) -> str | None:
        result = await db.execute(
            select(Project)
            .join(Organization, Organization.id == Project.organization_id)
            .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
            .where(Project.id == project_id, OrganizationMember.user_id == user_id)
        )
        project = result.scalar_one_or_none()
        if project is None:
            return None

        queues_result = await db.execute(
            select(Queue).options(selectinload(Queue.retry_policy)).where(Queue.project_id == project.id)
        )
        queues = list(queues_result.scalars().all())
        stats_by_queue = {q.id: await queue_stats(db, q.id) for q in queues}
        return templates.get_template("_project_queues_fragment.html").render(
            project=project, queues=queues, stats=stats_by_queue
        )

    await _ws_live_loop(websocket, db, render_tick)


@router.websocket("/ws/jobs/{job_id}")
async def ws_job_updates(websocket: WebSocket, job_id: int, db: AsyncSession = Depends(get_db)):
    """Live details / execution history / logs for the job detail page.
    The fragment's root carries data-status; the page script reloads once
    when the job reaches a terminal status so status-dependent chrome
    (retry button, AI-summary card) appears without re-implementing it
    client-side."""

    async def render_tick(db: AsyncSession, user_id: int) -> str | None:
        result = await db.execute(
            select(Job)
            .options(selectinload(Job.executions), selectinload(Job.logs))
            .join(Queue, Queue.id == Job.queue_id)
            .join(Project, Project.id == Queue.project_id)
            .join(Organization, Organization.id == Project.organization_id)
            .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
            .where(Job.id == job_id, OrganizationMember.user_id == user_id)
        )
        job = result.scalar_one_or_none()
        if job is None:
            return None
        return templates.get_template("_job_live_fragment.html").render(job=job)

    await _ws_live_loop(websocket, db, render_tick)


async def _user_watches(db: AsyncSession, user_id: int) -> list[Watch]:
    result = await db.execute(
        select(Watch)
        .join(OrganizationMember, OrganizationMember.organization_id == Watch.organization_id)
        .where(OrganizationMember.user_id == user_id)
        .order_by(Watch.created_at.desc())
    )
    return list(result.scalars().all())


@router.get("/dashboard/watches", response_class=HTMLResponse)
async def dashboard_watches(request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(require_web_user)):
    watches = await _user_watches(db, user.id)
    orgs_result = await db.execute(
        select(Organization)
        .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .where(OrganizationMember.user_id == user.id)
    )
    orgs = list(orgs_result.scalars().all())
    return templates.TemplateResponse(request, "watches.html", {"user": user, "watches": watches, "orgs": orgs})


async def _watch_detail_context(db: AsyncSession, watch: Watch) -> dict:
    stats = await watch_stats(db, watch.id)
    series = await watch_latency_series(db, watch.id)
    checks = list(
        (
            await db.execute(
                select(WatchCheck).where(WatchCheck.watch_id == watch.id).order_by(WatchCheck.started_at.desc()).limit(30)
            )
        ).scalars().all()
    )
    alerts = list(
        (
            await db.execute(
                select(WatchAlert).where(WatchAlert.watch_id == watch.id).order_by(WatchAlert.created_at.desc()).limit(20)
            )
        ).scalars().all()
    )
    return {"watch": watch, "stats": stats, "series": series, "checks": checks, "alerts": alerts}


@router.get("/dashboard/watches/{watch_id}", response_class=HTMLResponse)
async def dashboard_watch(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_web_user),
    watch: Watch = Depends(get_watch_for_user),
):
    context = await _watch_detail_context(db, watch)
    return templates.TemplateResponse(request, "watch_detail.html", {"user": user, **context})


@router.websocket("/ws/watches")
async def ws_watches_list(websocket: WebSocket, db: AsyncSession = Depends(get_db)):
    """Live watches table for the watches list page."""

    async def render_tick(db: AsyncSession, user_id: int) -> str:
        watches = await _user_watches(db, user_id)
        return templates.get_template("_watches_list_fragment.html").render(watches=watches)

    await _ws_live_loop(websocket, db, render_tick)


@router.websocket("/ws/watches/{watch_id}")
async def ws_watch_detail(websocket: WebSocket, watch_id: int, db: AsyncSession = Depends(get_db)):
    """Live stats/chart/history for the watch detail page."""

    async def render_tick(db: AsyncSession, user_id: int) -> str | None:
        result = await db.execute(
            select(Watch)
            .join(OrganizationMember, OrganizationMember.organization_id == Watch.organization_id)
            .where(Watch.id == watch_id, OrganizationMember.user_id == user_id)
        )
        watch = result.scalar_one_or_none()
        if watch is None:
            return None
        context = await _watch_detail_context(db, watch)
        return templates.get_template("_watch_live_fragment.html").render(**context)

    await _ws_live_loop(websocket, db, render_tick)


@router.get("/dashboard/jobs/{job_id}", response_class=HTMLResponse)
async def dashboard_job(
    request: Request,
    user: User = Depends(require_web_user),
    job: Job = Depends(get_job_for_user),
):
    return templates.TemplateResponse(request, "job_detail.html", {"user": user, "job": job})


@router.get("/dashboard/workers", response_class=HTMLResponse)
async def dashboard_workers(request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(require_web_user)):
    result = await db.execute(select(Worker).order_by(Worker.last_seen_at.desc()))
    workers = list(result.scalars().all())
    online_cutoff = datetime.now(timezone.utc) - timedelta(seconds=30)
    return templates.TemplateResponse(request, "workers.html", {"user": user, "workers": workers, "online_cutoff": online_cutoff})
