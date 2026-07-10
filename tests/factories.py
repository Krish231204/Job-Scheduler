"""Small helpers to build the org -> project -> queue chain tests need,
without dragging in the HTTP layer.
"""
from uuid import uuid4

from app.models import Organization, OrganizationMember, OrgRole, Project, Queue, RetryPolicy, RetryStrategy, User
from app.security import create_access_token, hash_password


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

    return await make_queue_in_project(
        db,
        project,
        max_concurrency=max_concurrency,
        priority=priority,
        strategy=strategy,
        max_retries=max_retries,
        base_delay_seconds=base_delay_seconds,
    )


async def make_queue_in_project(
    db,
    project: Project,
    *,
    name: str = "test-queue",
    max_concurrency: int = 4,
    priority: int = 0,
    strategy: RetryStrategy = RetryStrategy.FIXED,
    max_retries: int = 2,
    base_delay_seconds: float = 0.01,
) -> Queue:
    queue = Queue(project_id=project.id, name=name, priority=priority, max_concurrency=max_concurrency)
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


async def make_user_in_org(
    db,
    *,
    org: Organization | None = None,
    role: OrgRole = OrgRole.MEMBER,
    org_name: str = "Test Org",
) -> tuple[User, Organization, str]:
    """Creates a User (+ Organization if not given) + OrganizationMember with
    the given role, and returns (user, org, bearer_token) -- the token comes
    straight from app.security.create_access_token, the same function the
    real login endpoints use, so tests exercise real token verification
    without going through the rate-limited /auth/login endpoint."""
    if org is None:
        org = Organization(name=org_name)
        db.add(org)
        await db.flush()

    user = User(
        email=f"user-{uuid4().hex[:10]}@example.com",
        hashed_password=hash_password("password123"),
    )
    db.add(user)
    await db.flush()

    db.add(OrganizationMember(organization_id=org.id, user_id=user.id, role=role))
    await db.flush()

    token = create_access_token(subject=str(user.id))
    return user, org, token
