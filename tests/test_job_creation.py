from app.models import JobType
from app.services.job_service import create_batch, create_job
from tests.conftest import requires_db
from tests.factories import make_queue


@requires_db
async def test_idempotency_key_prevents_duplicate_job_creation(db_session):
    queue = await make_queue(db_session)
    first = await create_job(
        db_session, queue, name="send-email", job_type=JobType.IMMEDIATE, payload={"to": "a@example.com"},
        idempotency_key="welcome-email-user-42",
    )
    second = await create_job(
        db_session, queue, name="send-email", job_type=JobType.IMMEDIATE, payload={"to": "a@example.com"},
        idempotency_key="welcome-email-user-42",
    )
    assert first.id == second.id


@requires_db
async def test_batch_creates_one_job_per_item_sharing_a_batch_id(db_session):
    queue = await make_queue(db_session)
    items = [{"row": i} for i in range(4)]
    jobs = await create_batch(db_session, queue, name="import-row", items=items)

    assert len(jobs) == 4
    batch_ids = {j.batch_id for j in jobs}
    assert len(batch_ids) == 1
    assert all(j.job_type == JobType.BATCH for j in jobs)
