"""REST API for watches (the watcher app). Tenancy is org-level: creation
requires membership of the target organization, and every other endpoint
resolves the watch through the caller's memberships (a watch in another
org is a 404, indistinguishable from not existing -- same convention as
queues/jobs)."""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_watch_for_user
from app.models import OrganizationMember, User, Watch, WatchAlert, WatchCheck, WatchState
from app.rate_limit import limiter
from app.schemas import WatchAlertOut, WatchCheckOut, WatchCreate, WatchOut, WatchStats
from app.security import get_current_user
from app.services.stats import watch_stats
from app.services.url_safety import UrlPolicyError, validate_url_syntax
from app.services.watch_service import get_or_create_watch_queue

logger = logging.getLogger("jobsched.api.watches")
router = APIRouter(tags=["watches"])


@router.post("/watches", response_model=WatchOut, status_code=status.HTTP_201_CREATED)
@limiter.limit("30/minute")
async def create_watch(
    request: Request,
    payload: WatchCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    membership = (
        await db.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == payload.organization_id,
                OrganizationMember.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")

    try:
        validate_url_syntax(payload.url)
        if payload.webhook_url:
            validate_url_syntax(payload.webhook_url)
    except UrlPolicyError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    queue = await get_or_create_watch_queue(db, payload.organization_id)
    watch = Watch(
        organization_id=payload.organization_id,
        queue_id=queue.id,
        created_by=user.id,
        name=payload.name,
        url=payload.url,
        kind=payload.kind,
        keyword=payload.keyword.strip() if payload.keyword else None,
        interval_seconds=payload.interval_seconds,
        webhook_url=payload.webhook_url,
        next_check_at=datetime.now(timezone.utc),  # first check on the next scheduler tick
    )
    db.add(watch)
    await db.commit()
    await db.refresh(watch)
    logger.info("Watch created id=%s org=%s kind=%s url=%r", watch.id, watch.organization_id, watch.kind.value, watch.url)
    return watch


@router.get("/watches", response_model=list[WatchOut])
async def list_watches(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    result = await db.execute(
        select(Watch)
        .join(OrganizationMember, OrganizationMember.organization_id == Watch.organization_id)
        .where(OrganizationMember.user_id == user.id)
        .order_by(Watch.created_at.desc())
    )
    return list(result.scalars().all())


@router.get("/watches/{watch_id}", response_model=WatchOut)
async def get_watch(watch: Watch = Depends(get_watch_for_user)):
    return watch


@router.get("/watches/{watch_id}/stats", response_model=WatchStats)
async def get_watch_stats(db: AsyncSession = Depends(get_db), watch: Watch = Depends(get_watch_for_user)):
    return await watch_stats(db, watch.id)


@router.get("/watches/{watch_id}/checks", response_model=list[WatchCheckOut])
async def list_watch_checks(
    db: AsyncSession = Depends(get_db),
    watch: Watch = Depends(get_watch_for_user),
    limit: int = Query(default=50, ge=1, le=500),
):
    result = await db.execute(
        select(WatchCheck).where(WatchCheck.watch_id == watch.id).order_by(WatchCheck.started_at.desc()).limit(limit)
    )
    return list(result.scalars().all())


@router.get("/watches/{watch_id}/alerts", response_model=list[WatchAlertOut])
async def list_watch_alerts(
    db: AsyncSession = Depends(get_db),
    watch: Watch = Depends(get_watch_for_user),
    limit: int = Query(default=50, ge=1, le=500),
):
    result = await db.execute(
        select(WatchAlert).where(WatchAlert.watch_id == watch.id).order_by(WatchAlert.created_at.desc()).limit(limit)
    )
    return list(result.scalars().all())


@router.post("/watches/{watch_id}/pause", response_model=WatchOut)
async def pause_watch(db: AsyncSession = Depends(get_db), watch: Watch = Depends(get_watch_for_user)):
    watch.is_active = False
    await db.commit()
    await db.refresh(watch)
    logger.info("Watch paused id=%s", watch.id)
    return watch


@router.post("/watches/{watch_id}/resume", response_model=WatchOut)
async def resume_watch(db: AsyncSession = Depends(get_db), watch: Watch = Depends(get_watch_for_user)):
    watch.is_active = True
    if watch.state == WatchState.BROKEN:
        # Resuming a broken watch is a fresh start: clear the failure
        # streak and the last-fetch link so the next tick isn't judged by
        # the dead-lettered history that broke it.
        watch.state = WatchState.UNKNOWN
        watch.consecutive_failures = 0
        watch.last_fetch_job_id = None
    watch.next_check_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(watch)
    logger.info("Watch resumed id=%s", watch.id)
    return watch


@router.delete("/watches/{watch_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_watch(db: AsyncSession = Depends(get_db), watch: Watch = Depends(get_watch_for_user)):
    await db.delete(watch)  # checks/alerts cascade
    await db.commit()
    logger.info("Watch deleted id=%s", watch.id)
