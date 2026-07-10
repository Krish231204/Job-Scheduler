"""Cookie-based session helper for the server-rendered dashboard.

The dashboard is a thin HTML wrapper around the same JWT used by the REST
API: on login we set the JWT in an HttpOnly cookie, then a dependency reads
that cookie to authenticate each page render. This avoids maintaining two
parallel auth systems.
"""
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User
from app.security import SESSION_COOKIE_NAME, decode_token

COOKIE_NAME = SESSION_COOKIE_NAME


async def get_current_user_from_cookie(request: Request, db: AsyncSession = Depends(get_db)) -> User | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    try:
        user_id = decode_token(token)
    except HTTPException:
        return None
    result = await db.execute(select(User).where(User.id == int(user_id)))
    return result.scalar_one_or_none()


async def require_web_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    user = await get_current_user_from_cookie(request, db)
    if user is None:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    return user
