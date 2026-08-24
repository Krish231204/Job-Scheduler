# Job Scheduler + Watches

**Live:** [http://13.63.77.245/dashboard](http://13.63.77.245/dashboard) —
running on AWS EC2 (eu-north-1), including a real watch monitoring
[CortexOne](https://cortex-one-three.vercel.app) every 5 minutes.

A production-inspired platform for reliably executing asynchronous background
jobs across multiple workers — queues with priority/concurrency/retry config,
immediate/delayed/scheduled/recurring/batch submission and **DAG
dependencies**, an atomic-claim worker pool, retries with configurable
backoff, per-job execution timeouts, a dead letter queue, and a
server-rendered live dashboard — **plus a real application built on it:
Watches**, a multi-tenant URL watcher. Register a URL and a condition
(endpoint down / keyword appears / content changes) and the scheduler runs
each check as a fetch → diff → notify job pipeline, with retries,
transition-based idempotent alerting, and automatic dead-lettering of
permanently broken targets.

Stack: **FastAPI + SQLAlchemy (async) + PostgreSQL** for the API, a
**separate async worker process** for execution, a **separate scheduler
process** for time-based transitions, and **Jinja2** server-rendered pages
for the dashboard.

> **Origin:** this started as a take-home assignment and has been extended
> since — a security/hardening pass (org-scoped authorization, RBAC on queue
> config, rate limiting, liveness/readiness split), WebSocket live updates
> across the whole dashboard, a visual redesign with dark mode, throughput/
> latency metrics with reproducible benchmarks (see "Performance" below),
> a production deployment path for AWS EC2
> ([docs/DEPLOY_EC2.md](docs/DEPLOY_EC2.md)), job DAG dependencies and
> execution timeouts, the Watches application, AI-generated dead-letter
> failure summaries, and CI with linting plus dependency vulnerability
> scanning. See
> [docs/DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md) for what changed and
> why, including a **Known limitations** section covering the guarantees
> this system deliberately does *not* make.

## Repository layout

```
app/                  FastAPI app: routers, models, schemas, services
  routers/            auth, organizations, projects, queues, jobs, watches, workers, dashboard
  services/           job_service.py (state machine + DAG), watch_service.py,
                      url_safety.py (SSRF guard), retry.py, stats.py
  models.py           SQLAlchemy ORM models (see docs/ER_DIAGRAM.md)
worker/               Worker process: polls, claims, executes, heartbeats
  watch_handlers.py   fetch/diff/notify handlers (robots, rate limits, SSRF guard)
scheduler/            Scheduler process: cron/delay promotion, watch ticks, stale-worker recovery
templates/, static/   Server-rendered dashboard (Jinja2; light/dark, live over WebSockets)
migrations/           Alembic migrations
scripts/              seed.py (demo data), benchmark.py + watch_soak.py (see "Performance")
deploy/               Caddyfile for the optional HTTPS profile
tests/                Pytest suite (see "Testing" below)
docs/                 Architecture, ER diagram, design decisions, EC2 deploy guide
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

3. Watch it happen at `http://localhost:8000/dashboard` — queue health
   (including live jobs/sec and p50/p95/p99 latency), job explorer
   (filterable by status), execution logs/retry history per job, and
   worker status. Every page updates live over a WebSocket: the server
   re-renders the page's Jinja2 fragment and pushes HTML; there are no
   full-page refreshes and no client-side templates. The dashboard
   follows your OS light/dark preference, with a topbar toggle.

Jobs can also form a **DAG**: pass `depends_on: [job_ids]` and the job is
created `blocked`, runs only after every parent completes, and is skipped
(cancelled, with a log line saying why) if a parent dead-letters or is
cancelled. `timeout_seconds` puts a wall-clock limit on each execution
attempt — the worker cancels the handler and the attempt fails into the
normal retry path.

## Watches: the URL watcher built on the scheduler

`/dashboard/watches` is a complete multi-tenant application running on the
scheduler above. Register a URL, pick a condition and an interval, and the
scheduler materializes a **fetch → diff → notify job DAG** per check:

- **Conditions:** *endpoint down* (network error or HTTP ≥ 400), *keyword
  appears*, or *content changes* (whitespace-normalized SHA-256 of the
  body vs. the previous check).
- **Alerting is transition-based and idempotent:** one alert when a watch
  flips to triggered, one when it recovers — never one per check — and a
  unique dedupe key makes retries unable to duplicate them. Alerts show
  on the dashboard and optionally POST to a webhook.
- **Broken targets dead-letter themselves:** fetch failures retry per the
  watch queue's policy; a tick whose fetch dead-letters increments a
  failure streak, and three consecutive failed ticks mark the watch
  `broken`, deactivate it, and emit a final alert. Resuming clears the
  slate.
- **Polite and safe fetching:** robots.txt honored (cached per origin),
  per-domain rate limiting, bounded redirects and response sizes, and an
  SSRF guard — http/https only, port allowlist, and every resolved
  address must be public, so a watch can't probe the private network or
  the EC2 metadata service.
- **Live dashboard:** per-watch check history, 24h ok-rate and p50/p95
  check latency, and a live latency chart with outcome-colored points.

Each organization gets an auto-provisioned "Watches" project and
`watch-checks` queue, so all watch traffic is visible in the normal queue
dashboard, with the same retry/DLQ machinery as any other job.

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

## Performance

`scripts/benchmark.py` runs a fixed, deterministic workload (no randomness,
sequential idempotency keys, pinned 0.05s worker poll interval) against a
dedicated benchmark database and reports submission throughput, drain
throughput, and per-job latency percentiles — so any configuration change
can be quoted as a real before/after number:

```bash
python -m scripts.benchmark                              # 500 jobs, 1 worker x 8
python -m scripts.benchmark --jobs 1000 --workers 4 --concurrency 8
python -m scripts.benchmark --handler-ms 100 --json      # simulate real work; machine-readable
```

Numbers below were measured on a 4-vCPU Linux container (Python 3.11,
local PostgreSQL 16) with the flags shown — absolute values will differ on
your hardware; the *ratios* are the point.

**Worker-pool scaling** (500 jobs, 100 ms simulated work per job,
concurrency 4 per worker) — throughput scales near-linearly until the pool
approaches the scheduler's bookkeeping ceiling:

| Pool | Drain throughput | Execution p50 / p95 / p99 |
|---|---|---|
| 1 worker × 4 | 23.7 jobs/s | 115 / 119 / 132 ms |
| 2 workers × 4 | 46.4 jobs/s (1.96×) | 115 / 136 / 152 ms |
| 4 workers × 4 | 82.8 jobs/s (3.49×) | 118 / 142 / 187 ms |

**Scheduler overhead ceiling** (1000 jobs, 0 ms handler, 1 worker × 8):
~126 jobs/s drained. With no real work per job the bottleneck is the
claim/execute/record round trips to Postgres, so adding workers does not
raise it — a deliberate measurement of the framework's own cost per job.

**Cost of the idempotency guarantee** (1000 jobs, one commit per job,
1 worker × 8): 278 jobs/s submitted with idempotency keys vs 412 jobs/s
without — i.e. the partial unique index plus duplicate-check fast path
(migration 0003) costs about a third of raw submission throughput, which
is the price of exactly-one-job-per-key under concurrency.

**Watcher soak** (`scripts/watch_soak.py`): the full watcher pipeline —
scheduler materialization → fetch/diff/notify DAG → real worker
execution — against a deterministic local target, with tick cadence
accelerated so each watch re-checks as soon as its previous tick's DAG
finishes (acceleration changes how often checks run, not what each check
costs; latency percentiles are honest per-check numbers). 12 watches
across all three condition types, 90 s, same 4-vCPU container:

| Pool | Checks executed | Sustained rate | Check latency p50 / p95 / p99 |
|---|---|---|---|
| 1 worker × 8 | 1,124 (3,360 DAG jobs) | 12.5 checks/s | 21 / 33 / 37 ms |
| 2 workers × 8 | 1,634 (4,882 DAG jobs) | 18.1 checks/s | 22 / 29 / 68 ms |

Check latency covers the full guarded fetch: SSRF resolution check,
robots lookup (cached), and the HTTP round trip. At real-world intervals
(≥ 1 minute) a single t3.micro worker therefore has ~60× headroom over a
hundred active watches.

**Live production watch** (dogfooding): the EC2 deployment runs a real
`down` watch every 5 minutes against
[CortexOne](https://cortex-one-three.vercel.app), another of my deployed
projects (Vercel + Neon Postgres). The first 24 hours of data — **242
checks, 100% ok** — found a real bug, in the watcher itself: p50/p95 read
**10,175 / 10,354 ms**, implausibly constant for a Vercel app. That
constant is exactly the 10 s per-domain rate limit plus ~175 ms of actual
round trip: CortexOne answers its root URL with a redirect, and the fetch
loop was charging the domain limiter on *every hop*, so the second hop of
each check slept out nearly the full interval inside the measured window.
Fixed (a redirect chain now pays the limiter once per host, with a
regression test) — which is the point of running your own monitoring on
your own targets: an uptime tool whose latency numbers you never compare
against reality will happily report nonsense forever.

## Operations

- `GET /health/live` — process-alive check, no dependencies.
- `GET /health/ready` — checks the database is actually reachable
  (`SELECT 1`); returns 503 if not. Use this one for load-balancer/
  orchestrator readiness checks, not `/health/live`.
- Set `ENVIRONMENT=production` in a real deployment: this makes the app
  refuse to start if `JWT_SECRET` is still the insecure default, and makes
  the dashboard's session cookie `Secure` (HTTPS-only; `COOKIE_SECURE=false`
  is the documented opt-out for TLS-less deployments).
- Deploying for real: `docker-compose.prod.yml` (memory-capped services,
  secrets from `.env.prod`, optional Caddy HTTPS profile) plus the
  step-by-step AWS EC2 free-tier walkthrough in
  [docs/DEPLOY_EC2.md](docs/DEPLOY_EC2.md).

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

**Verification status:** this has been run end-to-end against real
Postgres -- registration, job submission (immediate + a
deliberately-failing job to watch the retry/dead-letter path), live
dashboard updates in a real browser, live watches exercised end to end
against a local target (all three condition types, transitions, alerts,
rate limiting), and the full `pytest` suite (54/54
passing on Python 3.11 and 3.13, including the concurrent-claim and
concurrent-idempotency tests) all confirmed working. A few real bugs
turned up only once it was actually executed (an enum serialization
mismatch, a missing `email-validator` dependency, a `passlib`/`bcrypt`
version incompatibility) and are documented, with fixes, in
docs/DESIGN_DECISIONS.md under "Verification history."

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — system architecture diagram and component responsibilities
- [docs/ER_DIAGRAM.md](docs/ER_DIAGRAM.md) — entity-relationship diagram and schema rationale
- [docs/DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md) — trade-offs and what's deliberately out of scope
- [docs/API.md](docs/API.md) — endpoint summary (full interactive docs at `/docs` once running)
