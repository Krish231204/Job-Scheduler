from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models import User

settings = get_settings()
# auto_error=False so requests carrying only the dashboard's session cookie
# (no Authorization header) don't get rejected before we can check the
# cookie ourselves in get_current_user below.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)
SESSION_COOKIE_NAME = "jobsched_session"

# bcrypt operates on at most 72 bytes of input. passlib (which this module
# used before it was retired -- unmaintained, and incompatible with
# bcrypt >= 4.1) silently truncated longer passwords, so existing hashes
# were produced from the first 72 bytes. Truncating here keeps every stored
# hash verifiable and newer bcrypt releases from raising on long input.
_BCRYPT_MAX_BYTES = 72


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode()[:_BCRYPT_MAX_BYTES], bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode()[:_BCRYPT_MAX_BYTES], hashed.encode())
    except ValueError:
        # Malformed/legacy hash in the database -- treat as non-matching
        # rather than turning a login attempt into a 500.
        return False


def create_access_token(subject: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {"sub": subject, "exp": expire}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> str:
    try:
        # algorithms= is the important argument here: pinning it to the one
        # algorithm we sign with is what stops a caller supplying a token
        # whose own header picks a weaker one (the classic JWT "alg"
        # confusion attack). PyJWT also rejects `alg: none` outright.
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        sub = payload.get("sub")
        if sub is None:
            raise ValueError("missing subject")
        return sub
    except (jwt.PyJWTError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


async def get_current_user(
    request: Request,
    token: str | None = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    # Prefer the Authorization: Bearer header (API clients); fall back to the
    # dashboard's HttpOnly session cookie so the server-rendered pages can
    # call these same REST endpoints from in-page fetch() calls.
    effective_token = token or request.cookies.get(SESSION_COOKIE_NAME)
    if not effective_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user_id = decode_token(effective_token)
    result = await db.execute(select(User).where(User.id == int(user_id)))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    return user
