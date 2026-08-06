import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_job_for_user, get_queue_for_user, get_scheduled_job_for_user
from app.models import DeadLetterEntry, Job, JobStatus, JobType, Queue, ScheduledJob
from app.rate_limit import limiter
from app.schemas import (
    AISummaryOut,
    JobCreate,
    JobDetailOut,
    JobOut,
    PaginatedJobs,
    ScheduledJobCreate,
    ScheduledJobOut,
)
from app.services.ai_summary import summarize_failure
from app.services.job_service import compute_initial_next_run, create_batch, create_job

logger = logging.getLogger("jobsched.api.jobs")
router = APIRouter(tags=["jobs"])


@router.post("/queues/{queue_id}/jobs", response_model=JobOut, status_code=status.HTTP_201_CREATED)
@limiter.limit("60/minute")
async def submit_job(
    request: Request,
    payload: JobCreate,
    db: AsyncSession = Depends(get_db),
    queue: Queue = Depends(get_queue_for_user),
):
    if payload.job_type == JobType.BATCH:
        jobs = await create_batch(
            db,
            queue,
            name=payload.name,
            items=payload.batch_items or [],
            priority=payload.priority,
            max_retries=payload.max_retries,
            retry_strategy=payload.retry_strategy,
        )
        await db.commit()
        logger.info("Batch submitted queue=%s batch_id=%s size=%s", queue.id, jobs[0].batch_id, len(jobs))
        # Return the first job of the batch; the batch_id groups the rest (see /jobs?batch_id=)
        result = await db.execute(select(Job).where(Job.id == jobs[0].id))
        return result.scalar_one()

    if payload.job_type == JobType.RECURRING:
        # Recurring jobs are created via a ScheduledJob definition, not a
        # bare Job row, so the cron continues firing indefinitely.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Use POST /queues/{queue_id}/scheduled-jobs with is_recurring=true for recurring jobs",
        )

    job = await create_job(
        db,
        queue,
        name=payload.name,
        job_type=payload.job_type,
        payload=payload.payload,
        priority=payload.priority,
        idempotency_key=payload.idempotency_key,
        delay_seconds=payload.delay_seconds,
        run_at=payload.run_at,
        max_retries=payload.max_retries,
        retry_strategy=payload.retry_strategy,
    )
    await db.commit()
    await db.refresh(job)
    logger.info("Job submitted id=%s queue=%s type=%s name=%r", job.id, queue.id, job.job_type.value, job.name)
    return job


@router.get("/queues/{queue_id}/jobs", response_model=PaginatedJobs)
async def list_jobs(
    db: AsyncSession = Depends(get_db),
    queue: Queue = Depends(get_queue_for_user),
    status_filter: JobStatus | None = Query(default=None, alias="status"),
    job_type: JobType | None = None,
    batch_id: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
):
    filters = [Job.queue_id == queue.id]
    if status_filter is not None:
        filters.append(Job.status == status_filter)
    if job_type is not None:
        filters.append(Job.job_type == job_type)
    if batch_id is not None:
        filters.append(Job.batch_id == batch_id)

    total = (await db.execute(select(func.count(Job.id)).where(*filters))).scalar_one()
    result = await db.execute(
        select(Job)
        .where(*filters)
        .order_by(Job.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return PaginatedJobs(items=list(result.scalars().all()), total=total, page=page, page_size=page_size)


@router.get("/jobs/{job_id}", response_model=JobDetailOut)
async def get_job(job: Job = Depends(get_job_for_user)):
    return job


@router.post("/jobs/{job_id}/retry", response_model=JobOut)
async def retry_job(db: AsyncSession = Depends(get_db), job: Job = Depends(get_job_for_user)):
    """Manually requeue a failed / dead-lettered job (e.g. from the dashboard)."""
    if job.status not in (JobStatus.DEAD_LETTER, JobStatus.FAILED, JobStatus.CANCELLED):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Cannot retry job in status {job.status}")
    previous_status = job.status
    job.status = JobStatus.QUEUED
    job.run_at = datetime.now(timezone.utc)
    job.claimed_by = None
    job.claimed_at = None
    await db.commit()
    await db.refresh(job)
    logger.info("Job manually retried id=%s (was %s)", job.id, previous_status.value)
    return job


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
async def cancel_job(db: AsyncSession = Depends(get_db), job: Job = Depends(get_job_for_user)):
    if job.status in (JobStatus.RUNNING, JobStatus.COMPLETED):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Cannot cancel job in status {job.status}")
    job.status = JobStatus.CANCELLED
    await db.commit()
    await db.refresh(job)
    logger.info("Job cancelled id=%s", job.id)
    return job


@router.post("/jobs/{job_id}/ai-summary", response_model=AISummaryOut)
async def get_ai_failure_summary(db: AsyncSession = Depends(get_db), job: Job = Depends(get_job_for_user)):
    """Generates (or returns the cached) plain-English failure summary for a
    dead-lettered job. Generated lazily on request rather than automatically
    for every failure, and cached on the DLQ entry, so this never fires more
    AI calls than a human actually asked to see."""
    if job.status != JobStatus.DEAD_LETTER:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="AI summaries are only available for dead-lettered jobs")

    result = await db.execute(select(DeadLetterEntry).where(DeadLetterEntry.job_id == job.id))
    dlq_entry = result.scalar_one_or_none()
    if dlq_entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No dead-letter entry found for this job")

    if dlq_entry.ai_summary:
        return AISummaryOut(summary=dlq_entry.ai_summary, cached=True)

    errors = [e.error for e in job.executions if e.error]
    summary = await summarize_failure(job.name, job.attempt_count, errors)
    dlq_entry.ai_summary = summary
    await db.commit()
    logger.info("AI failure summary generated for job id=%s", job.id)
    return AISummaryOut(summary=summary, cached=False)


# --------------------------------------------------------------------------
# Scheduled job definitions (recurring cron + one-off future scheduling)
# --------------------------------------------------------------------------

@router.post("/queues/{queue_id}/scheduled-jobs", response_model=ScheduledJobOut, status_code=status.HTTP_201_CREATED)
async def create_scheduled_job(
    payload: ScheduledJobCreate,
    db: AsyncSession = Depends(get_db),
    queue: Queue = Depends(get_queue_for_user),
):
    next_run = compute_initial_next_run(payload.cron_expression, payload.run_at, payload.is_recurring)
    sj = ScheduledJob(
        queue_id=queue.id,
        name=payload.name,
        job_name=payload.job_name,
        payload_template=payload.payload_template,
        cron_expression=payload.cron_expression,
        run_at=payload.run_at,
        is_recurring=payload.is_recurring,
        next_run_at=next_run,
    )
    db.add(sj)
    await db.commit()
    await db.refresh(sj)
    logger.info(
        "Scheduled job created id=%s queue=%s recurring=%s cron=%r next_run=%s",
        sj.id, queue.id, sj.is_recurring, sj.cron_expression, sj.next_run_at,
    )
    return sj


@router.get("/queues/{queue_id}/scheduled-jobs", response_model=list[ScheduledJobOut])
async def list_scheduled_jobs(db: AsyncSession = Depends(get_db), queue: Queue = Depends(get_queue_for_user)):
    result = await db.execute(select(ScheduledJob).where(ScheduledJob.queue_id == queue.id))
    return list(result.scalars().all())


@router.post("/scheduled-jobs/{scheduled_job_id}/pause", response_model=ScheduledJobOut)
async def pause_scheduled_job(db: AsyncSession = Depends(get_db), sj: ScheduledJob = Depends(get_scheduled_job_for_user)):
    sj.is_active = False
    await db.commit()
    await db.refresh(sj)
    logger.info("Scheduled job paused id=%s", sj.id)
    return sj


@router.post("/scheduled-jobs/{scheduled_job_id}/resume", response_model=ScheduledJobOut)
async def resume_scheduled_job(db: AsyncSession = Depends(get_db), sj: ScheduledJob = Depends(get_scheduled_job_for_user)):
    sj.is_active = True
    if sj.next_run_at is None or sj.next_run_at < datetime.now(timezone.utc):
        sj.next_run_at = compute_initial_next_run(sj.cron_expression, sj.run_at, sj.is_recurring)
    await db.commit()
    await db.refresh(sj)
    logger.info("Scheduled job resumed id=%s next_run=%s", sj.id, sj.next_run_at)
    return sj
