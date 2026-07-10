"""API-level auth/RBAC/tenant-isolation tests, driven through the real
FastAPI app (see the `api_client` fixture in conftest.py) rather than the
service layer directly.

These exist specifically to cover a class of bug the rest of the suite
never could: several endpoints (`GET /jobs/{id}`, retry/cancel, scheduled-
job pause/resume, `GET /workers`) originally had no auth dependency at all,
and three dashboard pages loaded records without checking org membership.
Both were only found by an external audit reading the router code, not by
running the existing tests -- because the existing tests never go through
routing/auth at all. See docs/DESIGN_DECISIONS.md for the full writeup.
"""
from app.models import JobType, OrgRole, Project
from app.security import SESSION_COOKIE_NAME
from app.services.job_service import create_job
from tests.conftest import requires_db
from tests.factories import make_queue_in_project, make_user_in_org


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@requires_db
async def test_get_job_requires_authentication(api_client, session_factory):
    async with session_factory() as db:
        _, org, _ = await make_user_in_org(db, role=OrgRole.OWNER)
        project = Project(organization_id=org.id, name="P")
        db.add(project)
        await db.flush()
        queue = await make_queue_in_project(db, project)
        job = await create_job(db, queue, name="t", job_type=JobType.IMMEDIATE, payload={})
        job_id = job.id
        await db.commit()

    resp = await api_client.get(f"/jobs/{job_id}")
    assert resp.status_code == 401


@requires_db
async def test_retry_and_cancel_require_authentication(api_client, session_factory):
    async with session_factory() as db:
        _, org, _ = await make_user_in_org(db, role=OrgRole.OWNER)
        project = Project(organization_id=org.id, name="P")
        db.add(project)
        await db.flush()
        queue = await make_queue_in_project(db, project)
        job = await create_job(db, queue, name="t", job_type=JobType.IMMEDIATE, payload={})
        job_id = job.id
        await db.commit()

    assert (await api_client.post(f"/jobs/{job_id}/retry")).status_code == 401
    assert (await api_client.post(f"/jobs/{job_id}/cancel")).status_code == 401


@requires_db
async def test_list_workers_requires_authentication(api_client):
    resp = await api_client.get("/workers")
    assert resp.status_code == 401


@requires_db
async def test_job_in_another_org_is_404_not_403(api_client, session_factory):
    async with session_factory() as db:
        _, org_a, _ = await make_user_in_org(db, role=OrgRole.OWNER, org_name="Org A")
        project_a = Project(organization_id=org_a.id, name="P")
        db.add(project_a)
        await db.flush()
        queue_a = await make_queue_in_project(db, project_a)
        job_a = await create_job(db, queue_a, name="t", job_type=JobType.IMMEDIATE, payload={})
        job_a_id = job_a.id
        queue_a_id = queue_a.id

        _, _, token_b = await make_user_in_org(db, role=OrgRole.OWNER, org_name="Org B")
        await db.commit()

    resp = await api_client.get(f"/jobs/{job_a_id}", headers=_auth(token_b))
    assert resp.status_code == 404

    resp = await api_client.get(f"/queues/{queue_a_id}", headers=_auth(token_b))
    assert resp.status_code == 404


@requires_db
async def test_member_cannot_pause_queue_but_admin_can(api_client, session_factory):
    async with session_factory() as db:
        _, org, _ = await make_user_in_org(db, role=OrgRole.OWNER, org_name="Org")
        project = Project(organization_id=org.id, name="P")
        db.add(project)
        await db.flush()
        queue = await make_queue_in_project(db, project)
        queue_id = queue.id

        _, _, owner_token = await make_user_in_org(db, org=org, role=OrgRole.OWNER)
        _, _, member_token = await make_user_in_org(db, org=org, role=OrgRole.MEMBER)
        await db.commit()

    resp = await api_client.post(f"/queues/{queue_id}/pause", headers=_auth(member_token))
    assert resp.status_code == 403

    resp = await api_client.post(f"/queues/{queue_id}/pause", headers=_auth(owner_token))
    assert resp.status_code == 200
    assert resp.json()["is_paused"] is True


@requires_db
async def test_dashboard_pages_404_for_another_orgs_ids(api_client, session_factory):
    async with session_factory() as db:
        _, org_a, _ = await make_user_in_org(db, role=OrgRole.OWNER, org_name="Org A")
        project_a = Project(organization_id=org_a.id, name="P")
        db.add(project_a)
        await db.flush()
        queue_a = await make_queue_in_project(db, project_a)
        job_a = await create_job(db, queue_a, name="t", job_type=JobType.IMMEDIATE, payload={})
        project_a_id, queue_a_id, job_a_id = project_a.id, queue_a.id, job_a.id

        _, _, token_b = await make_user_in_org(db, role=OrgRole.OWNER, org_name="Org B")
        await db.commit()

    api_client.cookies.set(SESSION_COOKIE_NAME, token_b)

    assert (await api_client.get(f"/dashboard/projects/{project_a_id}")).status_code == 404
    assert (await api_client.get(f"/dashboard/queues/{queue_a_id}")).status_code == 404
    assert (await api_client.get(f"/dashboard/jobs/{job_a_id}")).status_code == 404
