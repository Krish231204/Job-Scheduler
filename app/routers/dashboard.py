from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.database import get_db
from app.deps import get_job_for_user, get_project_for_user, get_queue_for_user, get_queue_role
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
    Worker,
)
from app.rate_limit import limiter
from app.security import create_access_token, hash_password, verify_password
from app.services.stats import queue_health_series, queue_stats
from app.web_auth import COOKIE_NAME, get_current_user_from_cookie, require_web_user

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory="templates")
settings = get_settings()


def _set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=settings.environment == "production",
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
