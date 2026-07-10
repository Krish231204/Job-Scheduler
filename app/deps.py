from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Organization, OrganizationMember, Project, Queue, User
from app.security import get_current_user


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
