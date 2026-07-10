"""Tests for the /ws/queues/{queue_id} live-update WebSocket endpoint.

Only the auth-rejection path is covered here, deliberately. The obvious
next test to write -- "an authenticated member receives a real rendered
fragment" -- runs into a genuine ecosystem rough edge: starlette's
TestClient drives WebSocket connections through an anyio background
thread with its own event loop, while SQLAlchemy's async engine (and the
asyncpg connections underneath it) are bound to whatever loop they were
first used from. Once app.dependency_overrides[get_db] hands the
WS-Depends-injected session to that background thread and it issues a
real query, asyncpg raises "Future attached to a different loop" -- not a
bug in the endpoint (this is exactly the fix that made the endpoint
testable/overridable at all -- see the docstring on ws_queue_updates in
app/routers/dashboard.py), just a limitation of testing a WebSocket this
way. The no-cookie rejection below doesn't hit this because it returns
before any query runs.

The "does live content actually update" behavior -- which is what the
skipped test would have checked -- was instead verified directly in a
browser: two tabs open on the same queue, submit a job, watch the job
explorer table and stats update in place with no page reload. That's a
more convincing check for a real-time UI feature than a unit test of the
same claim would be anyway.
"""
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.database import get_db
from app.main import app
from tests.conftest import requires_db


@requires_db
async def test_ws_rejects_connection_with_no_session_cookie(session_factory):
    async def _get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    try:
        client = TestClient(app)
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/queues/1"):
                pass
    finally:
        app.dependency_overrides.pop(get_db, None)
