import logging

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi.middleware import SlowAPIMiddleware
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import INSECURE_DEFAULT_JWT_SECRET, get_settings
from app.database import get_db
from app.errors import register_error_handlers
from app.rate_limit import limiter
from app.routers import auth, dashboard, jobs, orgs, projects, queues, workers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")

settings = get_settings()
# Minimum HMAC key length for HS256, per RFC 7518 section 3.2 ("A key of the
# same size as the hash output ... or larger MUST be used"). PyJWT warns
# about shorter keys; in production that should be a hard failure, since a
# short secret is brute-forceable offline by anyone holding one valid token.
_MIN_JWT_SECRET_BYTES = 32

if settings.environment == "production":
    # Fail loud at boot rather than silently signing every JWT with a
    # secret anyone can read in this repo's source/docs.
    if settings.jwt_secret == INSECURE_DEFAULT_JWT_SECRET:
        raise RuntimeError(
            "JWT_SECRET is still the insecure default while ENVIRONMENT=production. "
            "Set a real JWT_SECRET before starting in production."
        )
    if len(settings.jwt_secret.encode()) < _MIN_JWT_SECRET_BYTES:
        raise RuntimeError(
            f"JWT_SECRET is too short for {settings.jwt_algorithm} "
            f"({len(settings.jwt_secret.encode())} bytes; RFC 7518 requires at least "
            f"{_MIN_JWT_SECRET_BYTES}). Generate one with: python -c "
            "\"import secrets; print(secrets.token_urlsafe(48))\""
        )

app = FastAPI(
    title="Job Scheduler",
    description="A distributed job scheduling platform: queues, workers, retries, and a live dashboard.",
    version="1.0.0",
)

app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)

register_error_handlers(app)

app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(auth.router)
app.include_router(orgs.router)
app.include_router(projects.router)
app.include_router(queues.router)
app.include_router(jobs.router)
app.include_router(workers.router)
app.include_router(dashboard.router)


@app.get("/health/live")
async def health_live():
    """Process-alive check: no dependencies, always fast. For a liveness probe."""
    return {"status": "ok"}


@app.get("/health/ready")
async def health_ready(db: AsyncSession = Depends(get_db)):
    """Readiness check: confirms the DB is actually reachable. For a
    readiness probe / load-balancer health check -- unlike /health/live,
    this can correctly report "not ready" while the process is alive."""
    try:
        await db.execute(text("SELECT 1"))
    except Exception:
        logging.getLogger("jobsched.api").exception("Readiness check failed: database unreachable")
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"status": "ok"}
