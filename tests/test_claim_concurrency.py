"""The single most important reliability guarantee in this system: N workers
polling the same queue concurrently must never both claim the same job.

This exercises the real `SELECT ... FOR UPDATE SKIP LOCKED` path against a
real Postgres, with genuinely concurrent, independently-committed
transactions -- not just concurrent asyncio tasks sharing one transaction,
which wouldn't prove anything about row-level locking.
"""
import asyncio

from app.models import JobType, Worker, WorkerStatus
from app.services.job_service import claim_jobs, create_job
from tests.conftest import requires_db
from tests.factories import make_queue


@requires_db
async def test_concurrent_workers_never_claim_the_same_job(session_factory):
    num_jobs = 30
    num_workers = 6
    claim_limit_per_worker = 10  # 6 * 10 = 60 possible claims for 30 jobs: guarantees contention

    async with session_factory() as setup_session:
        queue = await make_queue(setup_session, max_concurrency=1000)
        job_ids = []
        for i in range(num_jobs):
            job = await create_job(setup_session, queue, name=f"job-{i}", job_type=JobType.IMMEDIATE, payload={})
            job_ids.append(job.id)

        workers = []
        for i in range(num_workers):
            w = Worker(name=f"worker-{i}", hostname="localhost", pid=1000 + i, status=WorkerStatus.ONLINE, concurrency=10)
            setup_session.add(w)
            workers.append(w)
        await setup_session.flush()
        worker_ids = [w.id for w in workers]
        queue_id = queue.id
        await setup_session.commit()

    async def claim_as_worker(worker_id: int) -> list[int]:
        async with session_factory() as session:
            claimed = await claim_jobs(session, worker_id, [queue_id], claim_limit_per_worker)
            claimed_ids = [j.id for j in claimed]
            await session.commit()
            return claimed_ids

    results = await asyncio.gather(*(claim_as_worker(wid) for wid in worker_ids))

    all_claimed = [job_id for worker_result in results for job_id in worker_result]

    # No duplicates: each job claimed by at most one worker.
    assert len(all_claimed) == len(set(all_claimed)), "the same job was claimed by more than one worker"
    # No losses: every job got claimed exactly once across the whole cluster.
    assert sorted(all_claimed) == sorted(job_ids)
