# Job Scheduler

A production-inspired platform for reliably executing asynchronous background
jobs across multiple workers: queues with priority/concurrency/retry config,
immediate/delayed/scheduled/recurring/batch job submission, an atomic-claim
worker pool, retries with configurable backoff, a dead letter queue, and a
server-rendered dashboard.

Stack: **FastAPI + SQLAlchemy (async) + PostgreSQL** for the API, a
**separate async worker process** for execution, a **separate scheduler
process** for time-based transitions, and **Jinja2** server-rendered pages
for the dashboard.

> **Origin:** this started as a take-home assignment and has been extended
> since — a security/hardening pass (org-scoped authorization, RBAC on queue
> config, rate limiting, liveness/readiness split), WebSocket live updates
> on the queue dashboard, AI-generated dead-letter failure summaries, and CI
> with linting plus dependency vulnerability scanning. See
> [docs/DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md) for what changed and
> why, including a **Known limitations** section covering the guarantees
> this system deliberately does *not* make.

## Repository layout

```
app/                  FastAPI app: routers, models, schemas, services
  routers/            auth, organizations, projects, queues, jobs, workers, dashboard
  services/           job_service.py (state machine), retry.py, stats.py
  models.py           SQLAlchemy ORM models (see docs/ER_DIAGRAM.md)
worker/               Worker process: polls, claims, executes, heartbeats
scheduler/            Scheduler process: cron/delay promotion, stale-worker recovery
templates/, static/   Server-rendered dashboard (Jinja2)
migrations/           Alembic migrations
tests/                Pytest suite (see "Testing" below)
docs/                 Architecture, ER diagram, design decisions
```

## Running it

### With Docker Compose (recommended)

```bash
docker compose up --build
# scale workers horizontally, e.g.:
docker compose up --build --scale worker=3
```

This starts Postgres, runs migrations, then brings up the API (port 8000),
1+ worker replicas, and the scheduler.

Open `http://localhost:8000/register` to create an account (this also
creates your first organization, a "Getting Started" project, and a
`general` queue). The API docs (OpenAPI/Swagger) are at
`http://localhost:8000/docs`.

### Demo data

To see the dashboard as it'd look for a system that's actually been running
a while (multiple projects, queues with varied config, jobs spread across
every status, workers, scheduled/cron jobs) rather than empty right after
signup, run:

```bash
docker compose exec api python -m scripts.seed
```

This attaches to whichever organization you already registered, so log in
first. Safe to re-run -- it skips projects that already exist by name.

### Locally, without Docker

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # edit DATABASE_URL if needed

# Postgres must be running and reachable at DATABASE_URL
alembic upgrade head

uvicorn app.main:app --reload           # terminal 1: API + dashboard
python -m worker.main --concurrency 8   # terminal 2: worker (run more of these to scale out)
python -m scheduler.main                # terminal 3: scheduler (run exactly one)
```

## Using it

1. Register at `/register` → you get a user, an organization (as owner),
   a default project, and a `default` queue with a sensible retry policy.
2. Submit jobs via the REST API, e.g. (login and job submission are both
   rate-limited -- 5/minute and 60/minute respectively -- so a scripted
   retry loop around either will eventually get a `429`):

```bash
TOKEN=$(curl -s -X POST localhost:8000/auth/login \
  -d "username=you@example.com&password=yourpassword" \
  -H "Content-Type: application/x-www-form-urlencoded" | jq -r .access_token)

curl -X POST localhost:8000/queues/1/jobs \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "send-email", "job_type": "immediate", "payload": {"to": "a@b.com"}}'

# Delayed
curl -X POST localhost:8000/queues/1/jobs -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "cleanup", "job_type": "delayed", "delay_seconds": 30, "payload": {}}'

# Recurring (cron) -- via the scheduled-jobs endpoint, not /jobs
curl -X POST localhost:8000/queues/1/scheduled-jobs -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "nightly-report", "job_name": "generate_report", "is_recurring": true, "cron_expression": "0 2 * * *", "payload_template": {}}'

# Batch
curl -X POST localhost:8000/queues/1/jobs -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "import-row", "job_type": "batch", "batch_items": [{"row": 1}, {"row": 2}]}'
```

3. Watch it happen at `http://localhost:8000/dashboard` — queue health,
   job explorer (filterable by status), execution logs/retry history per
   job, and worker status. The queue detail page updates live over a
   WebSocket (stats + job explorer refresh in place, no page reload) —
   everything else uses a periodic full-page refresh.

Jobs run through the built-in demo handler (`worker/handlers.py`) unless you
register a real one by name. The demo handler simulates work and can be told
to fail via `payload.simulate = "fail"` or `"flaky"` — handy for watching the
retry/DLQ path in the dashboard without writing a real job handler first.

**AI failure summaries:** a dead-lettered job's detail page has a "Get AI
summary" button that explains the likely cause in plain English. Works out
of the box with a rule-based fallback; set `ANTHROPIC_API_KEY` in `.env` (or
pass it through to the `api` service in `docker-compose.yml`) for real
Claude-generated summaries instead.

**Roles:** the user who registers/creates an organization is its `owner`.
Any org member can view queues/jobs and submit/retry/cancel jobs; changing
queue configuration (priority, concurrency, retry policy, pause/resume) or
creating new queues requires the `owner` or `admin` role.

## Operations

- `GET /health/live` — process-alive check, no dependencies.
- `GET /health/ready` — checks the database is actually reachable
  (`SELECT 1`); returns 503 if not. Use this one for load-balancer/
  orchestrator readiness checks, not `/health/live`.
- Set `ENVIRONMENT=production` in a real deployment: this makes the app
  refuse to start if `JWT_SECRET` is still the insecure default, and makes
  the dashboard's session cookie `Secure` (HTTPS-only).

## Testing

```bash
pip install -r requirements.txt
docker compose up -d db          # or point TEST_DATABASE_URL at any Postgres
export TEST_DATABASE_URL=postgresql+asyncpg://jobsched:jobsched@localhost:5432/jobsched_test
pytest
```

Pure-logic tests (retry backoff math, cron next-run calculation) run with no
database. The lifecycle, DLQ, and — most importantly — the concurrent-claim
tests need real Postgres, because they exercise `SELECT ... FOR UPDATE SKIP
LOCKED` row-locking semantics that SQLite doesn't replicate; they're
skipped automatically (not failed) if `TEST_DATABASE_URL` isn't reachable.

**Coverage:** `pip install pytest-cov && pytest --cov=app --cov=worker --cov=scheduler --cov-report=term-missing`
— currently **58% overall**, and deliberately uneven rather than uniformly
padded. `models.py` and `schemas.py` sit at 100%/93% and the pure logic in
`services/retry.py` at 93%, because those are cheap and valuable to cover.
Routers range from 31% to 75%: the tests that exist for them target
authorization and tenant isolation specifically (`tests/test_api_auth.py`),
not every branch of every handler. `worker/` and `scheduler/` report 0%
because their lifecycle and concurrency behavior is exercised through
`tests/test_lifecycle.py`, `tests/test_claim_concurrency.py` and
`tests/test_idempotency_concurrency.py` calling the same service functions
directly, rather than through the process entrypoints — those are verified
by actually running them (see "Verification status" below), which a
coverage number wouldn't capture either way.

Reporting the real spread rather than one flattering figure is deliberate:
the tests here are aimed at the two things that are genuinely hard to get
right in this system (concurrent claiming, and authorization), not at
maximizing a percentage.

**Lint & dependency scanning:** `pip install ruff pip-audit && ruff check .
&& pip-audit -r requirements.txt` (also run in CI on every push/PR).

**Verification status:** this has been run end-to-end via
`docker compose up --build` against real Postgres -- registration, job
submission (immediate + a deliberately-failing job to watch the
retry/dead-letter path), and the full `pytest` suite (33/33 passing,
including the concurrent-claim and concurrent-idempotency tests) all
confirmed working. A few real bugs
turned up only once it was actually executed (an enum serialization
mismatch, a missing `email-validator` dependency, a `passlib`/`bcrypt`
version incompatibility) and are documented, with fixes, in
docs/DESIGN_DECISIONS.md under "Verification history."

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — system architecture diagram and component responsibilities
- [docs/ER_DIAGRAM.md](docs/ER_DIAGRAM.md) — entity-relationship diagram and schema rationale
- [docs/DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md) — trade-offs and what's deliberately out of scope
- [docs/API.md](docs/API.md) — endpoint summary (full interactive docs at `/docs` once running)
