from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User, Worker, WorkerHeartbeat
from app.schemas import WorkerOut
from app.security import get_current_user

# Workers are cluster-wide shared infrastructure (they poll every unpaused
# queue across every org, see WorkerRunner._poll_once), not owned by a
# single organization/project -- so these endpoints require *some*
# authenticated user, not org membership like the project/queue/job routes.
router = APIRouter(prefix="/workers", tags=["workers"])


@router.get("", response_model=list[WorkerOut])
async def list_workers(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    result = await db.execute(select(Worker).order_by(Worker.last_seen_at.desc()))
    return list(result.scalars().all())


@router.get("/{worker_id}/heartbeats")
async def worker_heartbeats(
    worker_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user), limit: int = 50
):
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
