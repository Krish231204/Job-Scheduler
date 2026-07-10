import logging

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Organization, OrganizationMember, OrgRole, User
from app.schemas import OrganizationCreate, OrganizationOut
from app.security import get_current_user

logger = logging.getLogger("codity.api.orgs")
router = APIRouter(prefix="/organizations", tags=["organizations"])


@router.post("", response_model=OrganizationOut, status_code=status.HTTP_201_CREATED)
async def create_organization(
    payload: OrganizationCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    org = Organization(name=payload.name)
    db.add(org)
    await db.flush()
    db.add(OrganizationMember(organization_id=org.id, user_id=user.id, role=OrgRole.OWNER))
    await db.commit()
    await db.refresh(org)
    logger.info("Organization created id=%s name=%r by user=%s", org.id, org.name, user.id)
    out = OrganizationOut.model_validate(org)
    out.role = OrgRole.OWNER
    return out


@router.get("", response_model=list[OrganizationOut])
async def list_organizations(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    result = await db.execute(
        select(Organization, OrganizationMember.role)
        .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .where(OrganizationMember.user_id == user.id)
    )
    orgs = []
    for org, role in result.all():
        out = OrganizationOut.model_validate(org)
        out.role = role
        orgs.append(out)
    return orgs
