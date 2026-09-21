### Schema Migrations (`packages/harness/deerflow/persistence/migrations/`)

DeerFlow's current application schema has 12 tables (`runs`, `threads_meta`,
`feedback`, `users`, `run_events`, `scheduled_tasks`, `scheduled_task_runs`,
`agents`, `managed_subagents`, `personal_access_tokens`, `projects`, and
`user_preferences`). SQLite/PostgreSQL retain a development bootstrap for clean
databases. Production MySQL uses the independent `migrations_mysql/` chain and
is zero-DDL at Runtime; its application and checkpoint artifacts are applied by
DBA tooling. LangGraph's checkpointer tables (`checkpoints`,
`checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations`) live in the
same database but are owned by LangGraph and excluded from application
alembic's view via `migrations/_env_filters.py::include_object`.

**Convention**: every ORM model change (new column, new table, new index) MUST be reflected in the schema artifact that owns its backend. The historical SQLite/PostgreSQL revisions remain under `migrations/versions/`, but current Runtime does not replay them. MySQL changes live under `migrations_mysql/versions/`; Gateway never executes them.

**Fresh-schema bootstrap** (`persistence/bootstrap.py::bootstrap_schema`, invoked from `persistence/engine.py::init_engine` only for SQLite/PostgreSQL):

| DB state | Action |
| --- | --- |
| empty (no DeerFlow tables) | `create_all` + `alembic stamp head` |
| exactly one revision equal to current head | no-op |
| unversioned legacy/current-looking schema | fail closed without mutation |
| outdated, unknown, empty, or multiple revision rows | fail closed without migration |

The empty-DB path uses current `Base.metadata`, then stamps the immutable historical head. It never calls `alembic upgrade`. This preserves clean-checkout SQLite development and repository tests without reviving tables removed before the MySQL cutover. Existing databases require the documented fresh cutover or an explicitly audited offline procedure; Runtime never guesses an upgrade route. This bootstrap does not apply to MySQL.

The SQLite/PostgreSQL revision tree remains an immutable historical record for
audit and offline recovery. It is not a Runtime upgrade path. Do not restore an
`upgrade head` branch or a forward-revision allowlist in `bootstrap_schema`;
that would recreate retired Goal 0 tables and weaken fresh-cutover safety.

**Concurrency safety**: Postgres uses `pg_advisory_lock` to serialise concurrent clean-schema initialization. SQLite uses a per-engine `asyncio.Lock` for same-process startup and is best-effort across processes via SQLite's file-level write lock + `PRAGMA busy_timeout`; multi-instance deployments should not use SQLite.

**Authoring a new revision**:
```bash
cd backend && make migrate-rev MSG="add foo column to runs"
```
This command targets the historical SQLite/PostgreSQL tree. During the MySQL
migration, do not use it to create MySQL revisions; those belong to the
independent `migrations_mysql/` artifact and are never executed by Gateway.

**Extension-owned tables.** An extension that persists data owns its schema
end to end and must not register models against `deerflow.persistence.base.Base`
— doing so makes the host's empty-DB `create_all` create the extension's tables
on installs that never enabled it. The convention is:

- one `MetaData` instance private to the extension;
- every table sharing one prefix, declared via the `plugins:` record's
  `ExtensionSpec.table_prefix` field, so `alembic revision --autogenerate`
  ignores them instead of reflecting them, finding them absent from
  `Base.metadata`, and proposing `drop_table`. Registration happens in two
  places on purpose, because two different processes read the filter:
  `extensions/loader.py::load_extensions` covers the Gateway, and
  `register_configured_extension_table_prefixes()` — called from
  `migrations/env.py` — covers the alembic process, which never starts a
  Gateway and would otherwise see an empty prefix set exactly where
  `include_object` consumes it. The alembic side reads the declaration out of
  `config.yaml` and never imports extension code: a migration process must not
  execute third-party code. The Gateway side registers unconditionally, even
  for a disabled or later-failing spec, because the tables it names may
  already exist in the database from a previous run. Because those two readers
  cannot both be right about an empty prefix — the Gateway's truthiness test
  reads it as "no prefix", a literal reader as one matching every table —
  `ExtensionSpec.table_prefix` carries `min_length=1`, and the alembic-side
  reader (which parses raw YAML, so pydantic never runs there) skips anything
  that model would reject rather than raising: an operator does not expect to
  hear about a malformed `config.yaml` from alembic, and Gateway startup runs
  that same module through `bootstrap_schema`;

  Scope, because it is narrower than it first appears: **`make migrate-rev` is
  already safe without this.** `scripts/_autogen_revision.py` builds a
  throwaway SQLite from the migration chain and diffs against that, so no
  extension table — and no LangGraph table — is ever reflected. The exposed
  path is running `alembic revision --autogenerate` directly from the
  migrations directory, where `alembic.ini` points `sqlalchemy.url` at a real
  `./data/deerflow.db`. That is the same path `LANGGRAPH_OWNED_TABLES` covers,
  which is why that exclusion exists even though the throwaway-DB script
  landed in the same commit;
- an independent alembic chain with its own
  `version_table="<prefix>alembic_version"`, run from `ExtensionService.start()`
  against `ExtensionRuntimeDeps.session_factory`'s bind — which is sequenced
  after the host's own bootstrap by construction, since services start once
  persistence is ready;
- a Postgres advisory lock around that upgrade, mirroring `bootstrap_schema`,
  so concurrent Gateway instances serialise.

**Where things live**:
- `migrations/env.py` — alembic env, delegates filter to `_env_filters.py`, sets `render_as_batch=True` for SQLite ALTER support
- `migrations/_env_filters.py::include_object` — drops LangGraph checkpointer tables and any registered extension-owned tables (`EXTENSION_TABLE_PREFIXES`) from alembic's view
- `migrations/_env_filters.py::register_configured_extension_table_prefixes` — populates that set inside the alembic process, reading `plugins[*].table_prefix` from `config.yaml` and never importing extension code; called at import from `migrations/env.py`, because `load_extensions()` only ever runs in the Gateway
- `migrations/_helpers.py` — `safe_add_column` / `safe_drop_column`
- `migrations/versions/0001_baseline.py` — chain root, matches the schema `create_all` produces from `Base.metadata`
- `migrations/versions/0002_runs_token_usage.py` — fixes issue #3682
- `migrations/versions/0004_run_ownership.py` — `runs` multi-worker ownership + the `uq_runs_thread_active` partial unique index, with a `_dedupe_active_runs_per_thread()` pre-step so `CREATE UNIQUE INDEX` cannot fail on a field DB that already has duplicate active rows per thread
- `migrations/versions/0007_scheduled_run_active_index.py` — the `uq_scheduled_task_run_active` partial unique index (at most one queued/running `scheduled_task_runs` row per `task_id`), with a `_dedupe_active_scheduled_runs_per_task()` pre-step (keeps the newest active row per task, supersedes the rest to `interrupted` with an explanatory `error` + `finished_at`) mirroring 0004; chains after `0006_agents`
- `migrations/versions/0008_thread_operation_kind.py` — adds `runs.operation_kind` for durable non-run thread reservations; chains after `0007_scheduled_run_active_index`
- `migrations/versions/0010_run_cancel_request.py` — adds the nullable `runs.cancel_action` / `cancel_requested_at` handoff used by non-owning workers; chains after `0009_webhook_dedupe`
- `migrations/versions/0011_mcp_tasks.py` — creates the durable long-running MCP task table and its user/server/remote uniqueness constraint
- `migrations/versions/0012_mcp_task_results.py` — adds bounded result preview/truncation/artifact fields for ordinary task drivers
- `migrations/versions/0013_mcp_task_notifications.py` — adds durable Agent-run notification snapshots, delivery leases, idempotency fields, and the separate bounded-retry attempt counter
- `migrations/versions/0014_managed_subagents.py` — creates the deployment-level managed Subagent catalog table
- `migrations/versions/0015_scheduled_task_enqueue.py` — interrupts legacy transient queued rows, adds durable scheduled-run launch leases and attempt counts, expands the one-active-occurrence index to `queued`/`launching`/`running`, and migrates the overlap policy from `skip` to `enqueue`; chains after `0014_managed_subagents`
- `migrations/versions/0016_subagent_batches.py` — creates durable native-subagent batch and item tables, including owner/submission idempotency, item identity, lease/recovery state, and result fields
- `migrations/versions/0017_personal_access_tokens.py` — creates the personal access token table for programmatic API access
- `migrations/versions/0018_oauth_identity_pg_partial.py` — converts `idx_users_oauth_identity` to a partial index on Postgres (`postgresql_where`), matching what `UserRow.__table_args__` already builds via `create_all`; `0001_baseline` never applied the predicate on Postgres, so every `alembic upgrade head`-provisioned deployment carried a full index until this revision. Postgres-only, idempotent (checks `pg_index.indpred` directly), no-op on SQLite (already partial via `sqlite_where`) and on a DB where the index doesn't exist yet. Originally generated as 0017 and renumbered to 0018 after 0017_personal_access_tokens merged first and kept that slot
- `migrations/versions/0019_projects.py` — creates the `projects` table (id/user_id/name/instructions/presentation/status + timestamps) for the Projects Phase-1 organization feature; chains after `0018_oauth_identity_pg_partial`
- `migrations/versions/0020_threads_meta_project_id.py` — adds nullable `threads_meta.project_id` plus `ix_threads_meta_project_id` (no FK by design: project delete clears membership first, and the reserved `deerflow_project_id` metadata key stays in sync); chains after `0019_projects`
- `migrations/versions/0021_batch_acceptance.py` — adds nullable per-item acceptance criteria and verdict JSON columns after `0020_threads_meta_project_id`; legacy rows remain unchecked
- `migrations/versions/0019_thread_incarnations.py` — chains after `0021_batch_acceptance` while retaining the exact revision id audited by the rollback-floor binary. Adds nullable `threads_meta.incarnation` / `mcp_tasks.thread_incarnation` columns. New thread rows get a random 32-character incarnation. Memory mutations serialize per thread; an overwrite inherits the existing incarnation, while a delete/recreate gets a new one. SQLite MCP task INSERTs copy the owner-or-shared incarnation with a scalar subquery in the same statement. PostgreSQL task creation holds `FOR SHARE`, which conflicts with both current `FOR UPDATE` mutations and an older writer's plain owner update (`FOR NO KEY UPDATE`). Missing or differently owned threads store NULL, old writers may omit both columns, and current API/task serialization hides them. The migration preflights both tables before DDL and its SQLite downgrade cleans only safe remnants from its own interrupted batch-copy attempt
- `migrations/versions/0022_scheduled_occurrence_seq.py` — adds the per-task `last_occurrence_seq` high-water mark, nullable occurrence `occurrence_seq` and `launch_accounted`, and a unique `(task_id, occurrence_seq)` index. New occurrences allocate their sequence under the existing parent lock; launch accounting is recorded atomically with the count so an older recovered occurrence cannot be counted twice. Legacy child columns remain NULL without guessed ordering or accounting backfill. All three fields are internal and omitted from repository responses. Both once-task recovery paths lock the parent, defer while any occurrence row is active (sequenced or not), and otherwise project only from the highest sequence (`can_project`), the same rule as the launch, completion, and queue-failure writes. Chains after `0019_thread_incarnations` and is the current head.
- `persistence/bootstrap.py` — SQLite/PostgreSQL clean-schema creation, current-head no-op, and fail-closed rejection of every other state
- `extensions/loader.py::load_extensions` — registers each spec's `table_prefix` with `register_extension_table_prefix()`
- Tests: `tests/test_persistence_bootstrap_concurrency.py` (fresh/current/concurrency), `tests/test_persistence_bootstrap_regression.py` (legacy schemas remain untouched), the `test_persistence_bootstrap_{pg_lock,sqlite_lock,url}.py` focused contracts, `tests/test_persistence_migrations_env.py` (filter, including extension-owned tables), `tests/test_extension_loader.py::TestTablePrefixRegistration` (spec-to-filter wiring), and `tests/blocking_io/test_persistence_bootstrap.py` (asyncio.to_thread anchor)
