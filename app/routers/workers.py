from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Worker, WorkerHeartbeat
from app.schemas import WorkerOut

router = APIRouter(prefix="/workers", tags=["workers"])


@router.get("", response_model=list[WorkerOut])
async def list_workers(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Worker).order_by(Worker.last_seen_at.desc()))
    return list(result.scalars().all())


@router.get("/{worker_id}/heartbeats")
async def worker_heartbeats(worker_id: int, db: AsyncSession = Depends(get_db), limit: int = 50):
    result = await db.execute(
        select(WorkerHeartbeat)
        .where(WorkerHeartbeat.worker_id == worker_id)
        .order_by(WorkerHeartbeat.timestamp.desc())
        .limit(limit)
    )
    return [
        {"timestamp": hb.timestamp, "active_job_count": hb.active_job_count, "metrics": hb.metrics}
        for hb in result.scalars().all()
    ]
