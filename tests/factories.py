"""Small helpers to build the org -> project -> queue chain tests need,
without dragging in the HTTP layer.
"""
from app.models import Organization, Project, Queue, RetryPolicy, RetryStrategy


async def make_queue(
    db,
    *,
    max_concurrency: int = 4,
    priority: int = 0,
    strategy: RetryStrategy = RetryStrategy.FIXED,
    max_retries: int = 2,
    base_delay_seconds: float = 0.01,
) -> Queue:
    org = Organization(name="Test Org")
    db.add(org)
    await db.flush()

    project = Project(organization_id=org.id, name="Test Project")
    db.add(project)
    await db.flush()

    queue = Queue(project_id=project.id, name="test-queue", priority=priority, max_concurrency=max_concurrency)
    db.add(queue)
    await db.flush()

    retry_policy = RetryPolicy(
        queue_id=queue.id,
        strategy=strategy,
        max_retries=max_retries,
        base_delay_seconds=base_delay_seconds,
        multiplier=2.0,
        max_delay_seconds=60.0,
    )
    db.add(retry_policy)
    await db.flush()
    queue.retry_policy = retry_policy
    return queue
