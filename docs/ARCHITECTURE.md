# Architecture

## Components

```mermaid
flowchart LR
    subgraph Clients
        Browser["Dashboard (browser)"]
        API_Client["API client / script"]
    end

    subgraph "API process (FastAPI, N replicas, stateless)"
        REST["REST routers\nauth / orgs / projects / queues / jobs"]
        Dash["Dashboard routers\n(Jinja2 server-rendered)"]
    end

    subgraph "Worker processes (M replicas, horizontally scalable)"
        W1["Worker 1\npoll -> claim -> execute -> heartbeat"]
        W2["Worker 2"]
        Wn["Worker N"]
    end

    Scheduler["Scheduler process (single instance)\ncron/delay promotion, stale-worker recovery"]

    DB[("PostgreSQL\nqueues, jobs, executions, logs, workers, DLQ")]

    Browser --> Dash
    API_Client --> REST
    Dash --> DB
    REST --> DB
    W1 -- "SELECT ... FOR UPDATE SKIP LOCKED" --> DB
    W2 -- "SELECT ... FOR UPDATE SKIP LOCKED" --> DB
    Wn -- "SELECT ... FOR UPDATE SKIP LOCKED" --> DB
    Scheduler --> DB
```

![Component architecture diagram](images/architecture-components.png)

## Why three process types

- **API process** is stateless and horizontally scalable behind a load
  balancer; it only ever reads/writes Postgres, never talks to workers
  directly. This keeps the system's only shared state in one place, which
  is what makes atomic claiming possible in the first place.
- **Worker processes** are the thing you scale to increase throughput. Each
  is a single Python process running an asyncio loop; add more processes
  (or more machines) to add capacity. They talk to Postgres directly using
  the same SQLAlchemy models as the API (see `docs/DESIGN_DECISIONS.md` for
  why that's a deliberate choice, not an accident).
- **Scheduler** is a single lightweight loop most of the time doing nothing:
  it promotes due `ScheduledJob` definitions into concrete `Job` rows,
  promotes jobs whose retry backoff has elapsed from `RETRYING` back to
  `QUEUED`, and detects workers that have stopped sending heartbeats
  (crash/network partition) and requeues whatever they had claimed. None of
  this is performance-critical, so one instance is enough; if you need HA
  for it, run two behind a Postgres advisory lock (all of its writes are
  already idempotent/guarded with `SKIP LOCKED`, so a brief overlap during
  failover is harmless, just redundant work).

## Job flow (immediate job, happy path)

```mermaid
sequenceDiagram
    participant Client
    participant API
    participant DB as Postgres
    participant Worker

    Client->>API: POST /queues/1/jobs {job_type: immediate}
    API->>DB: INSERT Job(status=QUEUED, run_at=now)
    API-->>Client: 201 Job

    loop every poll_interval
        Worker->>DB: SELECT ... WHERE status IN (QUEUED,SCHEDULED)\nAND run_at <= now FOR UPDATE SKIP LOCKED
        DB-->>Worker: locked, unclaimed rows only
        Worker->>DB: UPDATE status=CLAIMED, claimed_by=worker_id
    end
    Worker->>DB: UPDATE status=RUNNING, INSERT JobExecution
    Worker->>Worker: run handler(payload)
    alt success
        Worker->>DB: UPDATE status=COMPLETED, execution.status=succeeded
    else failure
        Worker->>DB: compute backoff, UPDATE status=RETRYING or DEAD_LETTER
    end
```

![Job flow sequence diagram](images/architecture-job-flow.png)

## Request-path additions (rate limiting, health checks, optional AI calls)

- Every request through the API process passes through a `slowapi`
  rate-limiting middleware; only specific routes carry a limit
  (`/auth/login`, dashboard `/login`, job submission) -- most endpoints are
  unaffected. This is in-process/in-memory, not shared across API
  replicas; acceptable at this project's scale, and called out explicitly
  as a scaling limitation rather than left implicit.
- `/health/live` (no dependencies) and `/health/ready` (checks Postgres)
  are separate on purpose -- a load balancer or orchestrator should route
  on readiness, not liveness, or it'll keep sending traffic to a replica
  whose database connection is down but whose process is still alive.
- The queue detail dashboard page holds a persistent WebSocket connection
  (`/ws/queues/{queue_id}`) rather than the request/response pattern
  everything else in this diagram uses -- the API process re-queries
  Postgres and re-renders a fragment on an interval per open connection,
  pushing to that one client, rather than anything event-driven from the
  worker side. This keeps the worker/API separation intact (no new
  cross-process signaling to build) at the cost of a couple seconds of
  push latency, which is fine for a dashboard a human is watching.
- The AI failure-summary feature is the one place the API process makes
  an *outbound* call to something other than Postgres (the Anthropic API,
  only when a user clicks "Get AI summary" on a dead-lettered job, and
  only if `ANTHROPIC_API_KEY` is configured). It's synchronous from the
  request's perspective and falls back to a local rule-based summary on
  any failure -- see `docs/DESIGN_DECISIONS.md` -- so it never turns an
  external provider outage into a broken dashboard page, and never blocks
  the job lifecycle itself (which has nothing to do with this feature).

## Failure recovery

If a worker crashes mid-job, the DB still shows that job as `CLAIMED` or
`RUNNING` and its heartbeats stop arriving. The scheduler's stale-worker
sweep (`detect_stale_workers`, run every tick) marks the worker `OFFLINE` and
requeues every job it held back to `QUEUED`, so no job is silently lost --
the trade-off is that a crashed-but-still-executing job's side effects could
run twice, which is why job handlers should be idempotent (see
`idempotency_key` on `Job`, and `docs/DESIGN_DECISIONS.md`).
