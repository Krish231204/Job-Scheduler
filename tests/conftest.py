"""Test fixtures.

Most tests here need a real Postgres (SELECT ... FOR UPDATE SKIP LOCKED,
the concurrency behavior we're specifically testing, isn't meaningfully
exercised by SQLite). Point TEST_DATABASE_URL at a throwaway database, e.g.
via `docker compose up -d db` and:

    export TEST_DATABASE_URL=postgresql+asyncpg://jobsched:jobsched@localhost:5432/jobsched_test

Tests are skipped automatically if no test database is reachable, so
`pytest` still runs cleanly (skipping the DB-backed tests) in environments
without Postgres available -- e.g. plain retry/cron math tests still run.
"""
import asyncio
import os

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base, get_db

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://jobsched:jobsched@localhost:5432/jobsched_test"
)


def _db_available() -> bool:
    async def _check():
        engine = create_async_engine(TEST_DATABASE_URL)
        try:
            async with engine.connect():
                return True
        except Exception:
            return False
        finally:
            await engine.dispose()

    try:
        return asyncio.run(_check())
    except Exception:
        return False


requires_db = pytest.mark.skipif(not _db_available(), reason="TEST_DATABASE_URL is not reachable")


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(TEST_DATABASE_URL)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await eng.dispose()


@pytest_asyncio.fixture
async def db_session(engine):
    """A session bound to a transaction that's rolled back after the test,
    so each test starts from a clean slate without recreating the schema.
    """
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def session_factory(engine):
    """Raw sessionmaker for tests that need multiple independent, truly
    committed sessions (e.g. the concurrent-claim test).
    """
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def api_client(session_factory):
    """An httpx client wired directly to the real FastAPI app (via
    ASGITransport, no real socket) with get_db overridden to hand out
    sessions against the throwaway test database -- for tests that need to
    exercise actual routing/auth/dependency behavior, not just the service
    layer.
    """
    from app.main import app

    async def _override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.pop(get_db, None)
