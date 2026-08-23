from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Job, Organization, OrganizationMember, OrgRole, Project, Queue, ScheduledJob, User, Watch
from app.security import get_current_user

_ADMIN_ROLES = (OrgRole.OWNER, OrgRole.ADMIN)


async def require_org_member(
    organization_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Organization:
    result = await db.execute(
        select(Organization)
        .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .where(Organization.id == organization_id, OrganizationMember.user_id == user.id)
    )
    org = result.scalar_one_or_none()
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    return org


async def get_project_for_user(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Project:
    result = await db.execute(
        select(Project)
        .join(Organization, Organization.id == Project.organization_id)
        .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .where(Project.id == project_id, OrganizationMember.user_id == user.id)
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


async def get_queue_for_user(
    queue_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Queue:
    result = await db.execute(
        select(Queue)
        .options(selectinload(Queue.retry_policy))
        .join(Project, Project.id == Queue.project_id)
        .join(Organization, Organization.id == Project.organization_id)
        .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .where(Queue.id == queue_id, OrganizationMember.user_id == user.id)
    )
    queue = result.scalar_one_or_none()
    if queue is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Queue not found")
    return queue


async def get_job_for_user(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Job:
    result = await db.execute(
        select(Job)
        .options(selectinload(Job.executions), selectinload(Job.logs))
        .join(Queue, Queue.id == Job.queue_id)
        .join(Project, Project.id == Queue.project_id)
        .join(Organization, Organization.id == Project.organization_id)
        .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .where(Job.id == job_id, OrganizationMember.user_id == user.id)
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return job


async def get_scheduled_job_for_user(
    scheduled_job_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ScheduledJob:
    result = await db.execute(
        select(ScheduledJob)
        .join(Queue, Queue.id == ScheduledJob.queue_id)
        .join(Project, Project.id == Queue.project_id)
        .join(Organization, Organization.id == Project.organization_id)
        .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .where(ScheduledJob.id == scheduled_job_id, OrganizationMember.user_id == user.id)
    )
    sj = result.scalar_one_or_none()
    if sj is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scheduled job not found")
    return sj


async def get_watch_for_user(
    watch_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Watch:
    result = await db.execute(
        select(Watch)
        .join(OrganizationMember, OrganizationMember.organization_id == Watch.organization_id)
        .where(Watch.id == watch_id, OrganizationMember.user_id == user.id)
    )
    watch = result.scalar_one_or_none()
    if watch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Watch not found")
    return watch


async def get_project_admin(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Project:
    """Like get_project_for_user, but also requires an OWNER/ADMIN role --
    for project-level config actions (e.g. creating a queue), not plain
    membership actions (viewing, submitting jobs)."""
    result = await db.execute(
        select(Project, OrganizationMember.role)
        .join(Organization, Organization.id == Project.organization_id)
        .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .where(Project.id == project_id, OrganizationMember.user_id == user.id)
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    project, role = row
    if role not in _ADMIN_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Requires admin or owner role")
    return project


async def get_queue_role(db: AsyncSession, queue_id: int, user_id: int) -> OrgRole | None:
    """Non-raising role lookup for a queue's org, membership already
    established (e.g. by get_queue_for_user) -- used to decide whether to
    show admin-only controls in the dashboard rather than to gate access."""
    result = await db.execute(
        select(OrganizationMember.role)
        .join(Organization, Organization.id == OrganizationMember.organization_id)
        .join(Project, Project.organization_id == Organization.id)
        .join(Queue, Queue.project_id == Project.id)
        .where(Queue.id == queue_id, OrganizationMember.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def get_queue_admin(
    queue_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Queue:
    """Like get_queue_for_user, but also requires an OWNER/ADMIN role -- for
    queue config actions (update, pause, resume). Any member can still view
    a queue or submit/retry/cancel jobs against it (see get_queue_for_user)."""
    result = await db.execute(
        select(Queue, OrganizationMember.role)
        .options(selectinload(Queue.retry_policy))
        .join(Project, Project.id == Queue.project_id)
        .join(Organization, Organization.id == Project.organization_id)
        .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .where(Queue.id == queue_id, OrganizationMember.user_id == user.id)
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Queue not found")
    queue, role = row
    if role not in _ADMIN_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Requires admin or owner role")
    return queue
