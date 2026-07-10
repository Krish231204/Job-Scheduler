import logging

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.deps import get_project_for_user, get_queue_for_user
from app.models import Project, Queue, RetryPolicy
from app.schemas import QueueCreate, QueueOut, QueueStats, QueueUpdate
from app.services.stats import queue_stats

logger = logging.getLogger("codity.api.queues")
router = APIRouter(tags=["queues"])


@router.post("/projects/{project_id}/queues", response_model=QueueOut, status_code=status.HTTP_201_CREATED)
async def create_queue(
    project_id: int,
    payload: QueueCreate,
    db: AsyncSession = Depends(get_db),
    project: Project = Depends(get_project_for_user),
):
    queue = Queue(
        project_id=project.id,
        name=payload.name,
        priority=payload.priority,
        max_concurrency=payload.max_concurrency,
    )
    db.add(queue)
    await db.flush()

    retry_policy = RetryPolicy(queue_id=queue.id, **payload.retry_policy.model_dump())
    db.add(retry_policy)
    await db.commit()

    result = await db.execute(select(Queue).options(selectinload(Queue.retry_policy)).where(Queue.id == queue.id))
    created = result.scalar_one()
    logger.info("Queue created id=%s name=%r project=%s priority=%s concurrency=%s", created.id, created.name, project.id, created.priority, created.max_concurrency)
    return created


@router.get("/projects/{project_id}/queues", response_model=list[QueueOut])
async def list_queues(project_id: int, db: AsyncSession = Depends(get_db), project: Project = Depends(get_project_for_user)):
    result = await db.execute(
        select(Queue).options(selectinload(Queue.retry_policy)).where(Queue.project_id == project.id)
    )
    return list(result.scalars().all())


@router.get("/queues/{queue_id}", response_model=QueueOut)
async def get_queue(queue: Queue = Depends(get_queue_for_user)):
    return queue


@router.patch("/queues/{queue_id}", response_model=QueueOut)
async def update_queue(
    payload: QueueUpdate,
    db: AsyncSession = Depends(get_db),
    queue: Queue = Depends(get_queue_for_user),
):
    if payload.priority is not None:
        queue.priority = payload.priority
    if payload.max_concurrency is not None:
        queue.max_concurrency = payload.max_concurrency
    if payload.is_paused is not None:
        queue.is_paused = payload.is_paused
    if payload.retry_policy is not None:
        if queue.retry_policy is None:
            queue.retry_policy = RetryPolicy(queue_id=queue.id, **payload.retry_policy.model_dump())
        else:
            for k, v in payload.retry_policy.model_dump().items():
                setattr(queue.retry_policy, k, v)
    await db.commit()
    await db.refresh(queue)
    logger.info("Queue updated id=%s priority=%s concurrency=%s paused=%s", queue.id, queue.priority, queue.max_concurrency, queue.is_paused)
    return queue


@router.post("/queues/{queue_id}/pause", response_model=QueueOut)
async def pause_queue(db: AsyncSession = Depends(get_db), queue: Queue = Depends(get_queue_for_user)):
    queue.is_paused = True
    await db.commit()
    await db.refresh(queue)
    logger.info("Queue paused id=%s", queue.id)
    return queue


@router.post("/queues/{queue_id}/resume", response_model=QueueOut)
async def resume_queue(db: AsyncSession = Depends(get_db), queue: Queue = Depends(get_queue_for_user)):
    queue.is_paused = False
    await db.commit()
    await db.refresh(queue)
    logger.info("Queue resumed id=%s", queue.id)
    return queue


@router.get("/queues/{queue_id}/stats", response_model=QueueStats)
async def get_queue_stats(db: AsyncSession = Depends(get_db), queue: Queue = Depends(get_queue_for_user)):
    return await queue_stats(db, queue.id)
