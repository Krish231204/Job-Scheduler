# Entity-Relationship Diagram

```mermaid
erDiagram
    USERS ||--o{ ORGANIZATION_MEMBERS : "belongs to orgs via"
    ORGANIZATIONS ||--o{ ORGANIZATION_MEMBERS : has
    ORGANIZATIONS ||--o{ PROJECTS : owns
    PROJECTS ||--o{ QUEUES : contains
    QUEUES ||--o| RETRY_POLICIES : "configured by"
    QUEUES ||--o{ JOBS : holds
    QUEUES ||--o{ SCHEDULED_JOBS : defines
    SCHEDULED_JOBS ||--o{ JOBS : materializes
    JOBS ||--o{ JOB_EXECUTIONS : "attempted via"
    JOBS ||--o{ JOB_LOGS : produces
    JOB_EXECUTIONS ||--o{ JOB_LOGS : produces
    JOBS ||--o| DEAD_LETTER_ENTRIES : "may end in"
    WORKERS ||--o{ JOBS : claims
    WORKERS ||--o{ JOB_EXECUTIONS : runs
    WORKERS ||--o{ WORKER_HEARTBEATS : sends

    USERS {
        int id PK
        string email UK
        string hashed_password
        bool is_active
    }
    ORGANIZATIONS {
        int id PK
        string name
    }
    ORGANIZATION_MEMBERS {
        int id PK
        int organization_id FK
        int user_id FK
        enum role "owner/admin/member"
    }
    PROJECTS {
        int id PK
        int organization_id FK
        string name
    }
    QUEUES {
        int id PK
        int project_id FK
        string name
        int priority
        int max_concurrency
        bool is_paused
    }
    RETRY_POLICIES {
        int id PK
        int queue_id FK "unique"
        enum strategy "fixed/linear/exponential"
        int max_retries
        float base_delay_seconds
        float multiplier
        float max_delay_seconds
    }
    SCHEDULED_JOBS {
        int id PK
        int queue_id FK
        string cron_expression
        datetime run_at
        bool is_recurring
        datetime next_run_at
    }
    JOBS {
        int id PK
        int queue_id FK
        int scheduled_job_id FK
        string batch_id
        enum job_type "immediate/delayed/scheduled/recurring/batch"
        enum status "queued/scheduled/claimed/running/completed/failed/retrying/dead_letter/cancelled"
        json payload
        datetime run_at
        int attempt_count
        string idempotency_key
        int claimed_by FK
    }
    JOB_EXECUTIONS {
        int id PK
        int job_id FK
        int attempt_number
        int worker_id FK
        enum status "running/succeeded/failed"
        int duration_ms
    }
    JOB_LOGS {
        int id PK
        int job_id FK
        int execution_id FK
        enum level
        text message
    }
    DEAD_LETTER_ENTRIES {
        int id PK
        int job_id FK "unique"
        int queue_id FK
        text reason
        int attempt_count
        text ai_summary "nullable, cached on first-generated AI failure summary"
    }
    WORKERS {
        int id PK
        string hostname
        int pid
        enum status "online/draining/offline"
        int concurrency
    }
    WORKER_HEARTBEATS {
        int id PK
        int worker_id FK
        datetime timestamp
        int active_job_count
    }
```

![Entity-relationship diagram](images/er-diagram.png)

## Key design choices

**Primary keys.** Plain autoincrement integers everywhere. Simpler, smaller
indexes, and cheaper joins than UUIDs; nothing here needs to be globally
unique outside the database or generated client-side before insert.

**Cascades.** Ownership cascades all the way down
(`organization → project → queue → job → job_execution/job_log`): deleting a
queue deletes its jobs and their history, because orphaned job rows
referencing a deleted queue are meaningless. `Job.claimed_by → Worker` and
`JobExecution.worker_id → Worker` use `ON DELETE SET NULL` instead --
deleting a worker record shouldn't delete job history, it should just
disassociate it.

**Normalization.** Retry policy is its own table (one-to-one with Queue)
rather than columns bolted onto `Queue`, because jobs can override individual
fields (`max_retries_override`, `retry_strategy_override` on `Job`) and
keeping the "base" policy separate from the override made the fallback logic
in `_effective_retry_params` (see `app/services/job_service.py`) a lot
clearer than juggling nullable columns on two different tables.

**Indexes that matter for correctness/performance, not just uniqueness:**
- `ix_jobs_claim_lookup (queue_id, status, run_at)` — this is the index the
  worker's claim query hits on every poll cycle
  (`WHERE queue_id IN (...) AND status IN (queued, scheduled) AND run_at <=
  now() ORDER BY ... LIMIT n`). Without it, every poll from every worker is a
  full table scan on the busiest table in the system.
- `ix_jobs_idempotency (queue_id, idempotency_key)` — supports the
  idempotent-creation check in `create_job` without a full scan.
- `ix_scheduled_jobs_next_run (next_run_at)` — the scheduler's due-check
  query.
- `ix_worker_heartbeats_timestamp` — supports trimming/graphing heartbeat
  history without a scan.

**Why `ai_summary` lives on `DeadLetterEntry`, not `Job`.** The
AI-generated failure summary (see `docs/DESIGN_DECISIONS.md`) is only ever
meaningful for a job that's actually dead-lettered -- there's a natural
one-to-one fit with the row that already represents "this job is
permanently failed," rather than adding a nullable column to `Job` that's
meaningless for the other 8 statuses a job can be in.

**Why `Job` and `ScheduledJob` are separate tables** rather than one
self-referential table: a `ScheduledJob` is a *template* that can fire many
times (every occurrence of a cron job creates a new `Job` row linked back via
`scheduled_job_id`); collapsing them would mean overloading one row to mean
both "the definition" and "the most recent occurrence," which made querying
"show me all jobs from this schedule" and "is this schedule still active"
awkward together. Keeping them separate also matches the assignment's
explicit entity list.
