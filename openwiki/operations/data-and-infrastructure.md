---
type: operations
title: Data Layer and Production Infrastructure
description: How BossHunter uses PostgreSQL as the only production business and runtime database, Redis as the only cross-instance coordination substrate, Dramatiq-style registered tasks for safe background work, and local or S3 artifact stores for oversized tool results.
tags: [postgresql, redis, dramatiq, artifacts, runtime-store, migrations, fail-fast, coordination, data-layer]
verified:
  - by: openwiki/0.5.2
    at: 2026-09-16T06:31:53.644Z
---

# Data Layer and Production Infrastructure

BossHunter has one production data story and one production coordination story. PostgreSQL is the only durable business and Agent-runtime database in production, and Redis is the only cross-instance coordination facility. The design rule stated in `docs/生产基础设施重构.md` is the contract that governs the whole layer:

> BossHunter uses PostgreSQL as the only persistent database and Redis as the only runtime coordination facility. If either component is unavailable, the service fails to start; there is no fallback to SQLite or an in-process coordinator.

SQLite and the in-process coordinator still exist, but only on explicitly narrowed local-development and isolated-test paths. Production factories raise instead of silently degrading.

```mermaid
flowchart TD
    A["create_coordinator(config)"] --> B{"redis_url configured?"}
    B -->|no| C{"redis_required true?"}
    C -->|yes| D["RuntimeError"]
    C -->|no| E["LocalCoordinator"]
    B -->|yes| F["Build RedisCoordinator and ping"]
    F -->|ping ok| G["RedisCoordinator"]
    F -->|ping fails| H{"redis_required true?"}
    H -->|yes| I["raise"]
    H -->|no| J["LocalCoordinator"]
```

_Figure 1: coordination selection degrades to the in-process coordinator only when Redis is not configured and not required; any explicit multi-instance requirement fails fast._

## Component boundaries

| Component | Owns | Explicitly does not own |
| --- | --- | --- |
| PostgreSQL | jobs, history, sessions, LangGraph checkpoints, Agent runs, DAG steps, observations, approvals, and artifact references | schema changes at runtime (versioned migrations only) |
| Redis | session locks, cancel signals, and the real-time event stream | durable source-of-truth data |
| Dramatiq worker | execution of explicitly registered background tasks | arbitrary module/function import; automatic retry of send-like tasks |
| Artifact store | tool results and generated files that exceed the context budget | business state; it stores opaque bytes addressed by `storage_key` |

## PostgreSQL is the production database

`get_db()` in `src/bosshunter/db.py` is the business-database entry point. With no explicit `db_path`, it reads `database.url` and requires it to start with `postgresql://` or `postgresql+psycopg://`; anything else raises `RuntimeError` with the message that SQLite runtime degradation is disabled (`repo://src/bosshunter/db.py#L107-L125`). The `db_path` argument is used only to construct an explicit SQLite connection for isolated tests and never participates in the application runtime path.

`get_business_db(config_path, sqlite_fallback_path)` is the unified entry that the Web API and `AssistantService` must both call. It returns a `PostgresConnection` whenever a PostgreSQL URL is configured, and only reaches the explicit SQLite fallback path when no PostgreSQL URL is present — which exists for existing local development and isolated tests, not production (`repo://src/bosshunter/db.py#L128-L147`). The comment is operationally important: if the assistant wrote streamed answers to PostgreSQL while the conversation history read from a different SQLite file, the frontend would erase the message during the post-answer refresh.

The runtime store has no fallback at all. `create_runtime_store()` returns `PostgresRuntimeStore` for a PostgreSQL URL and otherwise raises `RuntimeError` explicitly saying SQLite runtime degradation is disabled (`repo://src/bosshunter/infrastructure/runtime_store.py#L355-L361`).

### The `PostgresConnection` adapter

`PostgresConnection` (`src/bosshunter/db.py#L12-L94`) gives the existing repositories a psycopg connection that behaves like the old SQLite connection. Its adaptation is narrow and deliberate:

- SQLite `?` placeholders are rewritten to psycopg `%s`, and SQLite named placeholders `:name` become `%(name)s`.
- `INSERT OR IGNORE INTO` becomes `INSERT INTO ... ON CONFLICT DO NOTHING`.
- SQLite date/time expressions (`strftime(...)`, `date('now')`, `datetime('now', '-N days')`) are rewritten to PostgreSQL expressions.
- Literal `%` characters inside quoted string literals (for example `LIKE` patterns) are escaped to `%%` so psycopg does not treat them as placeholders.
- `executescript()` is deliberately a no-op: PostgreSQL schema is owned by versioned migrations, and implicit runtime DDL is prohibited (`repo://src/bosshunter/db.py#L68-L70`).

Rows come back as `DatabaseRow`, a `dict` subclass that also supports positional integer access, so callers written against `sqlite3.Row` keep working (`repo://src/bosshunter/db.py#L97-L104`). The connection is constructed with a 10-second `connect_timeout` so an unreachable database fails quickly instead of hanging callers that may hold the global assistant lock (`repo://src/bosshunter/db.py#L32-L38`).

## Ordered migrations

PostgreSQL schema is owned by four numbered migrations under `migrations/postgresql/`, applied in order:

1. `001_agent_runtime.sql` creates the Agent runtime facts: `agent_runs`, `agent_plan_steps`, `agent_observations`, `action_proposals`, and `artifacts` (`repo://migrations/postgresql/001_agent_runtime.sql#L1-L56`).
2. `002_business_data.sql` creates the business and assistant tables: `jobs`, `history`, `risk_events`, `workbench_tasks`, `assistant_conversations`, `assistant_messages`, `assistant_memories`, `assistant_conversation_contexts`, `assistant_bad_cases`, and `assistant_message_ratings`, plus their indexes (`repo://migrations/postgresql/002_business_data.sql#L1-L82`).
3. `003_semantic_layer.sql` adds the structured funnel events table `job_events`, a PL/pgSQL trigger `record_job_status_event()`, and the read-only analysis views `semantic_job_current` and `semantic_job_funnel` (`repo://migrations/postgresql/003_semantic_layer.sql#L1-L76`).
4. `004_context_memory.sql` extends `assistant_memories` with the long-term-memory columns (`memory_key`, `category`, `value_json`, source metadata), backfills legacy keys as `legacy.<id>`, and adds the active-key unique index and the message cursor index (`repo://migrations/postgresql/004_context_memory.sql#L1-L30`).

The semantic layer is a database-side event store, not application-only bookkeeping. The trigger maps `jobs.status` transitions to typed events (`ai_scored`, `user_approved`, `greeting_sent`, `hr_replied`, `send_failed`, and so on) on both insert and status change (`repo://migrations/postgresql/003_semantic_layer.sql#L16-L51`). `semantic_job_current` is one row per job with its current state, and `semantic_job_funnel` produces first-arrival timestamps for each funnel stage using filtered `MIN(...)` aggregations (`repo://migrations/postgresql/003_semantic_layer.sql#L53-L72`).

Two idempotent migration scripts exist for pre-PostgreSQL data. `scripts/migrate_sqlite_to_postgresql.py` copies business tables in a fixed foreign-key-safe order with `ON CONFLICT DO NOTHING`, then resynchronizes the PostgreSQL identity sequences for `history`, `job_events`, and `risk_events` (`repo://scripts/migrate_sqlite_to_postgresql.py#L14-L37`, `repo://scripts/migrate_sqlite_to_postgresql.py#L83-L99`). `scripts/migrate_checkpoints_to_postgresql.py` replays SQLite LangGraph checkpoints into a `PostgresSaver` in parent-before-child order and groups pending writes by task (`repo://scripts/migrate_checkpoints_to_postgresql.py#L13-L50`).

## RuntimeStore: Agent run, DAG, observation, and approval facts

`RuntimeStore` in `src/bosshunter/infrastructure/runtime_store.py` is the persistence protocol for everything the assistant needs to recover and audit a run (`repo://src/bosshunter/infrastructure/runtime_store.py#L12-L40`). The production implementation is `PostgresRuntimeStore`, which intentionally does **not** create its own tables; the schema comes from `migrations/postgresql/001_agent_runtime.sql` so startup never mutates the database implicitly (`repo://src/bosshunter/infrastructure/runtime_store.py#L228-L233`).

```mermaid
erDiagram
    agent_runs ||--o{ agent_plan_steps : "has steps"
    agent_runs ||--o{ agent_observations : "has observations"
    agent_runs ||--o{ action_proposals : "has proposals"
    agent_runs ||--o{ artifacts : "references artifacts"
    agent_runs {
        UUID id PK
        TEXT conversation_id
        JSONB goal_json
        TEXT status
        TEXT error
    }
    agent_plan_steps {
        TEXT id PK
        UUID run_id FK
        TEXT capability
        JSONB arguments_json
        JSONB dependencies_json
        TEXT status
        JSONB result_json
        TEXT error
        TEXT idempotency_key UK
    }
    agent_observations {
        UUID id PK
        UUID run_id FK
        TEXT step_id
        JSONB observation_json
    }
    action_proposals {
        UUID id PK
        UUID run_id FK
        TEXT capability
        JSONB arguments_json
        JSONB approved_arguments_json
        TEXT status
        TIMESTAMPTZ expires_at
        TIMESTAMPTZ consumed_at
    }
    artifacts {
        TEXT id PK
        UUID run_id FK
        TEXT step_id
        TEXT storage_key UK
        TEXT content_type
        BIGINT size
        TEXT checksum
        JSONB metadata_json
    }
```

_Figure 2: the runtime facts are centered on `agent_runs`; steps, observations, proposals, and artifact references are children of one run._

The important semantics are:

- `save_step()` is idempotent. It upserts on `(run_id, id)` and always carries a stable `idempotency_key`, so re-running a plan step does not create duplicates (`repo://src/bosshunter/infrastructure/runtime_store.py#L266-L282`).
- `create_proposal()` freezes the proposal arguments, and `consume_proposal()` updates only a row that is still `pending`, returning `True` exactly once. The test pins this single-consume guarantee (`repo://src/bosshunter/infrastructure/runtime_store.py#L295-L317`, `repo://tests/test_agent_infrastructure.py#L27-L28`).
- `update_run()` records `finished_at` only for terminal statuses `completed`, `failed`, `cancelled`, and `budget_exhausted` (`repo://src/bosshunter/infrastructure/runtime_store.py#L256-L264`).
- `save_artifact()` stores only a lightweight reference — `storage_key`, content type, size, checksum, and metadata — never the bytes themselves (`repo://src/bosshunter/infrastructure/runtime_store.py#L340-L352`).

The `SQLiteRuntimeStore` mirrors this protocol for local mode and contains the same five tables, plus the `UNIQUE(idempotency_key)` and `UNIQUE(storage_key)` constraints (`repo://src/bosshunter/infrastructure/runtime_store.py#L62-L120`). It is the test double for the protocol, not a production fallback.

## Redis: coordination, never the source of truth

Redis handles exactly three runtime concerns, all under the `bosshunter:` key namespace:

1. **Session locks** — `lock(key)` acquires a distributed lock so one conversation runs at a time across instances.
2. **Cancel signals** — `request_cancel(run_id)` writes a one-hour key; `is_cancelled(run_id)` reads it.
3. **Event streams** — `publish_event(run_id, event)` appends to a Redis Stream capped at the last 2,000 entries.

`RedisCoordinator` implements all three (`repo://src/bosshunter/infrastructure/coordination.py#L74-L138`). Keys are `bosshunter:lock:<key>`, `bosshunter:run:<run_id>:cancel`, and `bosshunter:run:<run_id>:events`. Redis is deliberately not the system of record: durable state lives in PostgreSQL, and the event stream is an ephemeral push channel with a retention cap.

The lock has two safety mechanisms that matter for long-running Agents. It uses a lease of at least 30 seconds and a background renewal thread that extends the lease every third of its duration while the task runs; the renewal thread stops as soon as `lock.owned()` reports ownership was lost, so one instance can never renew another instance's lock (`repo://src/bosshunter/infrastructure/coordination.py#L88-L110`). On cleanup it releases the lock only if it still owns it, and swallows release errors so a lost lease cannot mask the Agent's real execution result (`repo://src/bosshunter/infrastructure/coordination.py#L114-L125`). The regression test pins that a lock whose ownership is already lost is not released again (`repo://tests/test_agent_infrastructure.py#L75-L110`).

`create_coordinator()` applies the fail-fast policy. With no `redis_url`, it returns `LocalCoordinator` unless `redis_required` is true, in which case it raises. With a URL, it builds `RedisCoordinator`, pings it, and only falls back to `LocalCoordinator` on failure when Redis was not required; when `redis_required` is true the exception propagates (`repo://src/bosshunter/infrastructure/coordination.py#L141-L157`). `LocalCoordinator` preserves the same lock/cancel/event semantics in-process for the single-process desktop mode (`repo://src/bosshunter/infrastructure/coordination.py#L29-L71`).

`AssistantService` wires this into the run lifecycle. It acquires `conversation:<id>` before invoking LangGraph, sets the cancel-check callback to `self._coordinator.is_cancelled(run_id)`, and publishes every normalized execution event to the coordinator stream (`repo://src/bosshunter/assistant/service.py#L135-L146`, `repo://src/bosshunter/assistant/service.py#L168-L172`). The cancel endpoint is `POST /api/assistant/runs/<run_id>/cancel`, which calls `AssistantService.cancel()` — that writes the Redis cancel signal and immediately marks the run `cancelled` in the runtime store (`repo://src/bosshunter/web/server.py#L1669-L1676`, `repo://src/bosshunter/assistant/service.py#L228-L231`).

Cancellation is cooperative, not preemptive. The DAG scheduler checks `cancel_check` before starting each parallel batch, marks remaining pending steps `skipped` with `用户取消任务`, and returns a `cancelled` run (`repo://src/bosshunter/assistant/dag_scheduler.py#L99-L105`). An external SDK call that is already running and cannot be interrupted safely is allowed to finish; no new nodes start afterward.

## Registered background tasks and Dramatiq

Background work never imports an arbitrary module or function path. `src/bosshunter/infrastructure/worker.py` maintains an in-memory `_TASK_HANDLERS` whitelist populated only by `register_task(name, handler)`, and `execute_registered_task()` refuses any name not in the whitelist (`repo://src/bosshunter/infrastructure/worker.py#L14-L26`). The focused test confirms that a name such as `os.system` is rejected even though it is a valid import path (`repo://tests/test_agent_infrastructure.py#L132-L133`).

Send-like tasks must not retry automatically, because an automatic retry would repeat an external side effect. `_NON_RETRYABLE_TASKS = {"send_greetings", "send_hr_reply", "run_pipeline"}` and `task_may_retry()` returns `False` exactly for those names (`repo://src/bosshunter/infrastructure/worker.py#L11`, `repo://src/bosshunter/infrastructure/worker.py#L29-L31`).

`configure_dramatiq(redis_url)` wires a Redis broker and declares a single generic actor `run_task(name, payload)` with `max_retries=0` and a 10-minute `time_limit`; the actor delegates to `execute_registered_task`, so the outer layer decides per task whether a retry is safe (`repo://src/bosshunter/infrastructure/worker.py#L34-L54`).

There is a deliberate separation between this worker boundary and the current live dispatch path. The Web assistant action dispatcher `_start_assistant_action()` in `src/bosshunter/web/server.py` maps confirmed proposals to the in-process `WorkbenchTaskRunner`, which runs each mode in a daemon thread with its own mutex and deadline rules (`repo://src/bosshunter/web/server.py#L360-L444`, `repo://src/bosshunter/web/tasks.py#L75-L122`). The `infrastructure/worker.py` module is the registered-task and Dramatiq-broker safety layer, currently wired in the package but not the dispatcher used by the assistant Web route.

## Artifact stores and the 16 KB offload

Tool results can be arbitrarily large, but Agent state should stay small. The artifact layer solves that by keeping only a reference in PostgreSQL and the bytes in a pluggable store.

`ArtifactStore` is a three-method protocol (`put`, `get`, `exists`). Both implementations return an `ArtifactReference` whose `id` is the SHA-256 checksum of the content — content-addressed, so the same bytes map to the same id (`repo://src/bosshunter/infrastructure/artifacts.py#L12-L33`).

- `LocalArtifactStore` restricts every key to the configured root. `_path()` rejects absolute paths, any `..` segment, and empty paths, and resolves the final path to ensure it stays under the root; `put()` writes a temporary file and atomically renames it into place (`repo://src/bosshunter/infrastructure/artifacts.py#L35-L67`).
- `S3ArtifactStore` uses `boto3` with the configured bucket and optional `endpoint_url`/`region`, publishing private objects and using `HEAD` for existence checks (`repo://src/bosshunter/infrastructure/artifacts.py#L78-L108`). Keys get the same traversal validation through `_normalize_key`.

`create_artifact_store()` selects S3 when `artifacts.backend == "s3"`, requiring a non-empty bucket and reading credentials from `BOSSHUNTER_S3_ACCESS_KEY` / `BOSSHUNTER_S3_SECRET_KEY`; otherwise it builds a `LocalArtifactStore` under `artifacts.local_path` (default `./data/artifacts`) (`repo://src/bosshunter/infrastructure/artifacts.py#L119-L134`).

The offload boundary is enforced in `AssistantService._persist_runtime_result()`. For each observation it serializes the raw tool result to UTF-8; if the byte size exceeds 16,384 bytes, it puts the bytes under `runs/<run_id>/<step_id>.json`, saves the returned artifact reference into the runtime store, clears `result` in the stored observation, and records `data_ref = reference["storage_key"]` so the summary survives without the oversized payload (`repo://src/bosshunter/assistant/service.py#L238-L251`).

## Configuration and operations

The infrastructure domains live in `DEFAULTS` and `config.example.yaml`, but are intentionally absent from the Web-editable schema (`repo://src/bosshunter/config.py#L129-L144`, `repo://config.example.yaml#L131-L148`):

```yaml
database:
  url: "postgresql+psycopg://user:password@127.0.0.1:5432/bosshunter"

runtime:
  redis_url: "redis://127.0.0.1:6379/0"
  redis_required: true
  worker_enabled: false
  max_concurrency: 4

artifacts:
  backend: "local"   # or "s3"
  local_path: "./data/artifacts"
  bucket: ""
  endpoint_url: ""
```

Operational checklist from `docs/生产基础设施重构.md`:

1. Create the PostgreSQL database.
2. Apply the migrations in numeric order; the third migration creates the structured job events, the status trigger, and the analysis views.
3. Set `database.url`.
4. For multi-instance deployment, set `runtime.redis_url` and `runtime.redis_required: true`; the desktop single-process mode can leave Redis unset.
5. Migrate legacy data with `scripts/migrate_sqlite_to_postgresql.py` and `scripts/migrate_checkpoints_to_postgresql.py`.

The failure posture is the operational invariant: PostgreSQL, Redis (when required), and a valid S3 configuration all fail fast, and no production code path silently swaps in an in-process substitute.

## Focused tests

`tests/test_agent_infrastructure.py` is the regression suite for this layer. It covers run/DAG/observation persistence and the single-consume proposal guarantee, the `create_runtime_store` PostgreSQL requirement, the local coordinator lock/cancel/event semantics, the coordinator factory's fail-fast and local fallback rules, the Redis lock ownership guard, local artifact path-escape rejection and checksumming, and the worker whitelist plus non-retryable send tasks (`repo://tests/test_agent_infrastructure.py#L13-L133`).
