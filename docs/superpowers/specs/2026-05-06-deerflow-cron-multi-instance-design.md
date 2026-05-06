# DeerFlow Cron Multi-Instance Design

## Background

`backend/packages/harness/deerflow` already has a mature run lifecycle, persistence layer, and background worker path. What it does not yet have is a cron scheduler that can:

- let users dynamically create, update, pause, resume, delete, and manually trigger schedules,
- dispatch scheduled work through the existing DeerFlow run lifecycle,
- run safely in multi-instance stateless deployments,
- minimize duplicate execution without requiring Redis on day one.

There is an existing MVP proposal in [bytedance/deer-flow#2624](https://github.com/bytedance/deer-flow/pull/2624). That PR is a useful reference for API shape and service boundaries, but it is designed around a single explicit leader process and store-backed polling. This spec targets a stronger requirement: all application instances may participate in scheduling, while the system still minimizes duplicate dispatches.

## Goals

- Support user-managed cron jobs through backend APIs and AI-agent-facing tools.
- Use the existing DeerFlow run launch path rather than introducing a second execution engine.
- Minimize duplicate execution in multi-instance stateless deployments.
- Recover safely from instance crashes during dispatch.
- Require only Postgres for the first production-ready version.
- Keep the design compatible with future Redis-based coordination enhancements.

## Non-Goals

- Frontend scheduling UI in the first version.
- Natural-language schedule parsing inside the scheduler itself.
- `at` and `every` schedule types in the first version.
- Retry policies, dead-letter queues, or catch-up replay of all missed fires.
- Cancelling already-started runs when a cron job is paused or deleted.

## User-Facing Scope

The first version supports:

- create schedule,
- update schedule,
- pause schedule,
- resume schedule,
- delete schedule,
- list schedules,
- manual trigger,
- cron-expression-based recurring dispatch,
- system-timezone-based scheduling.

The first version does not backfill missed executions after downtime. When the service resumes, scheduling continues from the current time forward.

## Core Semantics

### Timezone

- Jobs execute according to the system timezone.
- The persistence model still stores a timezone field so the schema remains extensible, but the first version writes and evaluates the system timezone only.

### Pause and Resume

- Pausing a job sets `enabled=false` and clears `next_fire_at`.
- Resuming a job recalculates `next_fire_at` from the current time.
- Missed executions while paused are not replayed.

### Cron Updates

- Updating the cron expression recalculates `next_fire_at` from the update time.
- Missed executions under the previous cron are not replayed.

### Manual Trigger

- Manual trigger creates a run immediately.
- Manual trigger does not advance the wall-clock schedule and does not consume the next scheduled fire slot.

### Missed Fires

- The first version uses `catch_up = none` semantics.
- After downtime or long dispatch delays, the scheduler moves forward from the present instead of replaying all missed schedule points.

## High-Level Architecture

The cron system is split into four layers:

1. `Schedule API / Tool Layer`
   Receives structured create, update, pause, resume, delete, list, and manual-trigger requests from backend routes or DeerFlow tools.

2. `Schedule Persistence Layer`
   Stores cron job definitions and per-fire dispatch records in SQL tables managed by DeerFlow persistence.

3. `Scheduler Loop`
   Runs in every application instance. Each instance scans due jobs, but only one instance can claim a specific scheduled fire.

4. `Dispatch Layer`
   Launches scheduled work through the existing DeerFlow run lifecycle. The scheduler never executes agent logic directly.

This preserves the main architectural advantage of PR #2624: scheduling remains a thin launcher over the existing run path.

## Comparison with PR #2624

PR #2624 already gets several important things right:

- dynamic backend CRUD for cron jobs,
- manual trigger support,
- reuse of the existing run launch path,
- a narrow backend-only MVP scope,
- scheduler-specific metadata injection into run metadata.

This design intentionally reuses those ideas.

This design differs from PR #2624 in the following key ways:

- It does not rely on a single designated leader instance.
- It does not use the generic DeerFlow Store as the primary scheduling state backend.
- It introduces a separate per-fire dispatch table to make scheduled dispatch claimable and auditable.
- It relies on SQL uniqueness and lease semantics to minimize duplicate dispatch.
- It adds a second layer of protection through run-launch idempotency.

In short, PR #2624 is a good single-leader skeleton, but not sufficient as-is for multi-instance low-duplication scheduling.

## Data Model

### `cron_jobs`

Represents a user-managed schedule definition.

Suggested fields:

- `job_id`
- `thread_id`
- `assistant_id`
- `cron_expr`
- `timezone`
- `enabled`
- `input_json`
- `metadata_json`
- `config_json`
- `context_json`
- `multitask_strategy`
- `next_fire_at`
- `last_fire_at`
- `last_run_id`
- `version`
- `created_at`
- `updated_at`

Responsibilities:

- store the schedule definition,
- store the next wall-clock fire time,
- expose job state for APIs and tools.

### `cron_job_fires`

Represents a single scheduled fire instance for a specific job at a specific scheduled timestamp.

Suggested fields:

- `fire_id`
- `job_id`
- `scheduled_fire_at`
- `status`
- `claim_owner`
- `claim_token`
- `lease_until`
- `run_id`
- `error`
- `created_at`
- `updated_at`

Suggested `status` values:

- `claimed`
- `dispatched`
- `dispatch_failed`
- `expired`

Required uniqueness constraint:

- `UNIQUE(job_id, scheduled_fire_at)`

This uniqueness constraint is the core low-duplication mechanism. It turns each scheduled fire into a claimable unit instead of relying only on mutable job-level timestamps.

## Dispatch and Claim Flow

Each application instance runs a scheduler loop. The loop operates as follows:

1. Scan `cron_jobs` where `enabled=true` and `next_fire_at <= now`.
2. For each due job, attempt to create or claim a `cron_job_fires` record for `(job_id, scheduled_fire_at=next_fire_at)`.
3. Only the instance that succeeds in claiming the fire may continue dispatch.
4. The claiming instance writes `claim_owner`, `claim_token`, and `lease_until`.
5. The claiming instance launches a DeerFlow run through the existing run path.
6. On successful launch, update the fire record to `dispatched` and store `run_id`.
7. Advance `cron_jobs.next_fire_at` to the next schedule point.
8. On launch failure, set the fire record to `dispatch_failed` and preserve error details.

The critical design rule is that claim uniqueness is enforced at the database layer, not through an in-memory lock and not through a single elected leader.

## Lease and Crash Recovery

If an instance crashes after claiming a fire but before completing dispatch:

- other instances may observe a `claimed` fire whose `lease_until < now`,
- the fire may be transitioned to `expired`,
- the system may then retry dispatch of the same `scheduled_fire_at`.

To make this safe, retries for expired fires must use run-launch idempotency.

The first version should support automatic recovery rather than requiring manual cleanup, because otherwise a crash between claim and dispatch would permanently drop a scheduled execution.

## Run Idempotency

The scheduler must not rely on claim uniqueness alone. It also needs execution-layer idempotency.

Each scheduled dispatch should attach stable scheduler metadata to the run launch request:

- `scheduler.job_id`
- `scheduler.scheduled_fire_at`
- `scheduler.fire_id`
- `scheduler.idempotency_key = cron:{job_id}:{scheduled_fire_at}`

The cron launch path should check whether a run already exists for the same idempotency key in an active or terminal-success state. If so, it should return or reuse the existing run instead of creating another one.

This is the final guard against duplicate execution during recovery races or partial failures.

## API and Tool Design

### Backend API

The backend API should follow the shape established by PR #2624:

- `POST /api/cron/jobs`
- `GET /api/cron/jobs`
- `GET /api/cron/jobs/{job_id}`
- `PATCH /api/cron/jobs/{job_id}`
- `DELETE /api/cron/jobs/{job_id}`
- `POST /api/cron/jobs/{job_id}/trigger`

This shape is already understandable and maps well to user-managed schedules.

### DeerFlow Tools

For AI-agent-facing management, expose structured built-in tools instead of requiring the agent to handcraft raw API calls:

- `create_schedule`
- `update_schedule`
- `pause_schedule`
- `resume_schedule`
- `delete_schedule`
- `list_schedules`
- `trigger_schedule`

The agent is responsible for understanding the user's intent. The tool layer is responsible for structured validation and execution.

## Authorization and Safety Limits

The first version should enforce:

- users may only manage schedules for threads and assistants they are authorized to use,
- maximum schedules per user or thread,
- minimum allowed schedule frequency,
- payload size limits,
- auditability of creator and updater identity.

These limits matter more once schedules can be created through AI conversation.

## Persistence and Implementation Location

This work should live primarily under `backend/packages/harness/deerflow` and integrate with the existing persistence system.

Suggested module layout:

- `deerflow/runtime/scheduler/`
- `deerflow/persistence/scheduler/`
- migration files under `deerflow/persistence/migrations/versions/`
- gateway router and service wiring mirroring the route shape from PR #2624

The scheduler should reuse existing runtime and run-launch wiring rather than introducing a second execution stack.

## Testing Strategy

Testing should cover four layers.

### 1. Schema and Service Unit Tests

Cover:

- cron validation,
- system-timezone next-fire calculation,
- pause and resume semantics,
- cron update semantics,
- manual trigger semantics.

### 2. Repository Tests

Cover:

- creating jobs,
- scanning due jobs,
- uniqueness on `(job_id, scheduled_fire_at)`,
- lease expiration and recovery,
- fire status transitions.

### 3. Concurrency Integration Tests

This is the most important layer.

Simulate multiple scheduler workers concurrently scanning the same due jobs and assert:

- only one valid fire claim wins for a given scheduled fire,
- run launch occurs at most once, or duplicate launch attempts are absorbed by run idempotency,
- crash recovery after claim works correctly.

### 4. Gateway and Tool Integration Tests

Cover:

- CRUD behavior,
- manual trigger,
- authorization,
- interaction between API mutations and scheduler wake-up behavior.

## Failure Cases to Explicitly Test

- two instances discover the same due job at the same time,
- an instance crashes after claim but before run launch,
- an instance crashes after run launch but before fire or job state is fully updated,
- a job is paused while a dispatch attempt is in progress,
- a job is updated while a dispatch attempt is in progress,
- a transient database error rolls back a claim or state transition,
- a retry path encounters an existing run with the same idempotency key.

## Redis Extension Path

The first version should not depend on Redis, but the claim coordination boundary should be abstracted so Redis can be added later.

Suggested abstraction:

- `DispatchCoordinator`

First implementation:

- `PostgresDispatchCoordinator`

Future enhancement:

- `RedisLeaseCoordinator` for faster lease operations or distributed throttling,
- Postgres remains the source of truth for jobs, fires, and audit state.

Redis should accelerate coordination, not replace durable scheduling state.

## Recommended Delivery Sequence

1. Add scheduler SQL models and migration.
2. Add repository and service logic for jobs and fires.
3. Add claim and lease flow on top of Postgres.
4. Add run-launch idempotency for cron dispatch.
5. Add gateway CRUD and manual-trigger routes.
6. Add DeerFlow built-in schedule management tools.
7. Add concurrency and recovery integration tests.

## Open Design Choices Resolved in This Spec

To avoid ambiguity, this spec fixes the following behaviors:

- system timezone is the only supported runtime timezone in v1,
- missed fires are not backfilled,
- manual trigger does not advance wall-clock schedule,
- paused or deleted jobs do not cancel already-started runs,
- automatic recovery of expired claims is allowed,
- run-launch idempotency is required for safe recovery.

## Summary

The recommended first production-ready design is:

- inspired by PR #2624 at the API and service-boundary level,
- implemented with SQL-backed job and fire persistence,
- multi-instance safe through database claim uniqueness and leases,
- protected by a second layer of run-launch idempotency,
- Redis-ready without requiring Redis initially.

This gives DeerFlow a cron foundation that supports user-managed schedules today and leaves room for later extensions such as reminder-style schedules, Redis acceleration, catch-up policies, and delivery integrations.
