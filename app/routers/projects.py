import logging

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_project_for_user, require_org_member
from app.models import Organization, Project, User
from app.schemas import ProjectCreate, ProjectOut
from app.security import get_current_user

logger = logging.getLogger("codity.api.projects")
router = APIRouter(tags=["projects"])


@router.post("/organizations/{organization_id}/projects", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(
    organization_id: int,
    payload: ProjectCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    org: Organization = Depends(require_org_member),
):
    project = Project(organization_id=org.id, name=payload.name, description=payload.description, created_by=user.id)
    db.add(project)
    await db.commit()
    await db.refresh(project)
    logger.info("Project created id=%s name=%r org=%s by user=%s", project.id, project.name, org.id, user.id)
    return project


@router.get("/organizations/{organization_id}/projects", response_model=list[ProjectOut])
async def list_projects(
    organization_id: int,
    db: AsyncSession = Depends(get_db),
    org: Organization = Depends(require_org_member),
):
    result = await db.execute(select(Project).where(Project.organization_id == org.id))
    return list(result.scalars().all())


@router.get("/projects/{project_id}", response_model=ProjectOut)
async def get_project(project: Project = Depends(get_project_for_user)):
    return project
