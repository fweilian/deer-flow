# DeerFlow Cron Multi-Instance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a SQL-backed, multi-instance-safe cron scheduler for DeerFlow that supports user-managed schedules, low-duplication dispatch, and existing-run-path execution.

**Architecture:** Add scheduler persistence under DeerFlow's SQL persistence layer, represent each scheduled occurrence as a claimable fire record, and run a lightweight scheduler loop on every instance. Dispatch reuses the existing run launch path and adds a cron-specific idempotency check so crash recovery can retry safely without double-executing work.

**Tech Stack:** FastAPI, Pydantic, SQLAlchemy async ORM, Alembic, pytest, Postgres-first persistence, existing DeerFlow runtime/run manager.

---

## File Structure

### New files

- `backend/packages/harness/deerflow/persistence/scheduler/__init__.py`
  Export scheduler ORM models and repository helpers.
- `backend/packages/harness/deerflow/persistence/scheduler/model.py`
  Define `CronJobRow` and `CronJobFireRow`.
- `backend/packages/harness/deerflow/persistence/scheduler/sql.py`
  Implement CRUD, due-job scanning, fire claim, fire completion, and recovery helpers.
- `backend/packages/harness/deerflow/runtime/scheduler/__init__.py`
  Re-export scheduler schemas and service entry points.
- `backend/packages/harness/deerflow/runtime/scheduler/schemas.py`
  Define create/update/record payloads and validation helpers.
- `backend/packages/harness/deerflow/runtime/scheduler/service.py`
  Implement scheduler orchestration, due-job dispatch, claim/recovery flow, and manual trigger path.
- `backend/app/gateway/cron_scheduler.py`
  Wire scheduler lifecycle into Gateway startup and build the non-HTTP run launcher.
- `backend/app/gateway/routers/cron.py`
  Provide backend cron CRUD and manual-trigger endpoints.
- `backend/packages/harness/deerflow/tools/builtins/schedule_tool.py`
  Expose built-in schedule management tools for DeerFlow agents.
- `backend/packages/harness/deerflow/persistence/migrations/versions/<revision>_add_cron_scheduler_tables.py`
  Create `cron_jobs` and `cron_job_fires` tables plus indexes and uniqueness constraints.
- `backend/tests/test_cron_scheduler_schemas.py`
  Validate cron schema behavior and next-fire semantics.
- `backend/tests/test_cron_scheduler_repository.py`
  Cover SQL repository behavior.
- `backend/tests/test_cron_scheduler_service.py`
  Cover scheduler loop, claiming, manual trigger, and recovery.
- `backend/tests/test_cron_router.py`
  Cover cron CRUD and trigger endpoints.
- `backend/tests/test_schedule_tool.py`
  Cover built-in schedule tools.
- `backend/tests/test_cron_dispatch_idempotency.py`
  Cover run-launch idempotency behavior for cron dispatch.

### Modified files

- `backend/packages/harness/deerflow/persistence/models/__init__.py`
  Import scheduler ORM models so metadata and autoload discover them.
- `backend/packages/harness/deerflow/persistence/__init__.py`
  Re-export scheduler persistence components if the package pattern expects it.
- `backend/packages/harness/deerflow/runtime/__init__.py`
  Re-export scheduler schemas and service classes.
- `backend/app/gateway/app.py`
  Register cron router and scheduler startup/shutdown hooks.
- `backend/app/gateway/routers/__init__.py`
  Export the new cron router.
- `backend/app/gateway/services.py`
  Add a dependency-free run launch helper and cron dispatch idempotency check.
- `backend/packages/harness/deerflow/persistence/run/sql.py`
  Add lookup by metadata idempotency key for cron dispatch reuse.
- `backend/packages/harness/deerflow/tools/builtins/__init__.py`
  Export the new schedule tool.

## Task 1: Add Scheduler Schemas and SQL Models

**Files:**
- Create: `backend/packages/harness/deerflow/runtime/scheduler/schemas.py`
- Create: `backend/packages/harness/deerflow/persistence/scheduler/model.py`
- Create: `backend/packages/harness/deerflow/persistence/scheduler/__init__.py`
- Modify: `backend/packages/harness/deerflow/persistence/models/__init__.py`
- Modify: `backend/packages/harness/deerflow/runtime/__init__.py`
- Test: `backend/tests/test_cron_scheduler_schemas.py`

- [ ] **Step 1: Write the failing schema test**

```python
from deerflow.runtime.scheduler.schemas import CronJobCreate, compute_next_fire_at


def test_resume_uses_system_timezone(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    payload = CronJobCreate(
        thread_id="thread-1",
        assistant_id="lead_agent",
        cron="0 9 * * *",
        timezone="Asia/Shanghai",
    )

    next_fire = compute_next_fire_at(payload.cron, payload.timezone, now=1_746_500_000)
    assert next_fire > 1_746_500_000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_cron_scheduler_schemas.py::test_resume_uses_system_timezone -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'deerflow.runtime.scheduler'`

- [ ] **Step 3: Write minimal scheduler schemas**

```python
class CronJobCreate(BaseModel):
    thread_id: str
    assistant_id: str | None = None
    cron: str
    timezone: str
    enabled: bool = True
    input: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] | None = None
    context: dict[str, Any] | None = None
    multitask_strategy: Literal["reject", "interrupt", "rollback", "enqueue"] = "enqueue"


class CronJobRecord(CronJobCreate):
    job_id: str
    next_fire_at: float | None = None
    last_fire_at: float | None = None
    last_run_id: str | None = None
    created_at: float
    updated_at: float
```

```python
class CronJobRow(Base):
    __tablename__ = "cron_jobs"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    assistant_id: Mapped[str | None] = mapped_column(String(128))
    cron_expr: Mapped[str] = mapped_column(String(128), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    input_json: Mapped[dict] = mapped_column(JSON, default=dict)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    config_json: Mapped[dict] = mapped_column(JSON, default=dict)
    context_json: Mapped[dict] = mapped_column(JSON, default=dict)
    multitask_strategy: Mapped[str] = mapped_column(String(20), default="enqueue")
    next_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_id: Mapped[str | None] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(default=1, nullable=False)
```

```python
class CronJobFireRow(Base):
    __tablename__ = "cron_job_fires"
    __table_args__ = (UniqueConstraint("job_id", "scheduled_fire_at", name="uq_cron_job_fires_job_sched"),)

    fire_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("cron_jobs.job_id", ondelete="CASCADE"), nullable=False, index=True)
    scheduled_fire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="claimed")
    claim_owner: Mapped[str | None] = mapped_column(String(128))
    claim_token: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    run_id: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
```

- [ ] **Step 4: Run schema tests**

Run: `cd backend && uv run pytest tests/test_cron_scheduler_schemas.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/packages/harness/deerflow/runtime/scheduler/schemas.py \
        backend/packages/harness/deerflow/persistence/scheduler/model.py \
        backend/packages/harness/deerflow/persistence/scheduler/__init__.py \
        backend/packages/harness/deerflow/persistence/models/__init__.py \
        backend/packages/harness/deerflow/runtime/__init__.py \
        backend/tests/test_cron_scheduler_schemas.py
git commit -m "feat: add cron scheduler schemas and models"
```

## Task 2: Add Migration and Repository CRUD

**Files:**
- Create: `backend/packages/harness/deerflow/persistence/scheduler/sql.py`
- Create: `backend/packages/harness/deerflow/persistence/migrations/versions/<revision>_add_cron_scheduler_tables.py`
- Test: `backend/tests/test_cron_scheduler_repository.py`

- [ ] **Step 1: Write the failing repository test**

```python
async def test_create_job_and_list_due_jobs(session_factory):
    repo = CronSchedulerRepository(session_factory)
    record = await repo.create_job(
        CronJobCreate(
            thread_id="thread-1",
            assistant_id="lead_agent",
            cron="*/5 * * * *",
            timezone="Asia/Shanghai",
        ),
        now=1_746_500_000,
    )

    due = await repo.list_due_jobs(now=record.next_fire_at)
    assert [job.job_id for job in due] == [record.job_id]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_cron_scheduler_repository.py::test_create_job_and_list_due_jobs -v`

Expected: FAIL with `NameError: name 'CronSchedulerRepository' is not defined`

- [ ] **Step 3: Implement repository CRUD and due-job scanning**

```python
class CronSchedulerRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def create_job(self, payload: CronJobCreate, *, now: float | None = None) -> CronJobRecord:
        current_time = _as_utc(now)
        row = CronJobRow(
            job_id=str(uuid.uuid4()),
            thread_id=payload.thread_id,
            assistant_id=payload.assistant_id,
            cron_expr=payload.cron,
            timezone=payload.timezone,
            enabled=payload.enabled,
            input_json=payload.input or {},
            metadata_json=payload.metadata,
            config_json=payload.config or {},
            context_json=payload.context or {},
            multitask_strategy=payload.multitask_strategy,
            next_fire_at=_dt_or_none(compute_next_fire_at(payload.cron, payload.timezone, now=now)) if payload.enabled else None,
            created_at=current_time,
            updated_at=current_time,
        )
        async with self._sf() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return self._job_row_to_record(row)
```

```python
async def list_due_jobs(self, *, now: float | None = None, limit: int = 100) -> list[CronJobRecord]:
    current_time = _as_utc(now)
    stmt = (
        select(CronJobRow)
        .where(CronJobRow.enabled.is_(True), CronJobRow.next_fire_at.is_not(None), CronJobRow.next_fire_at <= current_time)
        .order_by(CronJobRow.next_fire_at.asc(), CronJobRow.created_at.asc())
        .limit(limit)
    )
    async with self._sf() as session:
        rows = (await session.execute(stmt)).scalars().all()
    return [self._job_row_to_record(row) for row in rows]
```

```python
def upgrade() -> None:
    op.create_table(
        "cron_jobs",
        sa.Column("job_id", sa.String(length=64), primary_key=True),
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column("cron_expr", sa.String(length=128), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("next_fire_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_cron_jobs_enabled_next_fire_at", "cron_jobs", ["enabled", "next_fire_at"])
    op.create_table(
        "cron_job_fires",
        sa.Column("fire_id", sa.String(length=64), primary_key=True),
        sa.Column("job_id", sa.String(length=64), sa.ForeignKey("cron_jobs.job_id", ondelete="CASCADE"), nullable=False),
        sa.Column("scheduled_fire_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("job_id", "scheduled_fire_at", name="uq_cron_job_fires_job_sched"),
    )
```

- [ ] **Step 4: Run repository tests**

Run: `cd backend && uv run pytest tests/test_cron_scheduler_repository.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/packages/harness/deerflow/persistence/scheduler/sql.py \
        backend/packages/harness/deerflow/persistence/migrations/versions/*_add_cron_scheduler_tables.py \
        backend/tests/test_cron_scheduler_repository.py
git commit -m "feat: add cron scheduler repository and migration"
```

## Task 3: Add Fire Claim, Lease, and Recovery Logic

**Files:**
- Modify: `backend/packages/harness/deerflow/persistence/scheduler/sql.py`
- Create: `backend/packages/harness/deerflow/runtime/scheduler/service.py`
- Create: `backend/packages/harness/deerflow/runtime/scheduler/__init__.py`
- Test: `backend/tests/test_cron_scheduler_service.py`

- [ ] **Step 1: Write the failing claim test**

```python
async def test_claim_due_fire_is_unique(session_factory):
    repo = CronSchedulerRepository(session_factory)
    service = CronSchedulerService(repo, run_launcher=AsyncMock(), instance_id="worker-a")
    job = await repo.create_job(
        CronJobCreate(
            thread_id="thread-1",
            assistant_id="lead_agent",
            cron="*/5 * * * *",
            timezone="Asia/Shanghai",
        ),
        now=1_746_500_000,
    )

    first = await repo.claim_fire(job.job_id, scheduled_fire_at=job.next_fire_at, instance_id="worker-a", lease_seconds=30)
    second = await repo.claim_fire(job.job_id, scheduled_fire_at=job.next_fire_at, instance_id="worker-b", lease_seconds=30)

    assert first is not None
    assert second is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_cron_scheduler_service.py::test_claim_due_fire_is_unique -v`

Expected: FAIL with `AttributeError: 'CronSchedulerRepository' object has no attribute 'claim_fire'`

- [ ] **Step 3: Implement claim and recovery**

```python
async def claim_fire(
    self,
    job_id: str,
    *,
    scheduled_fire_at: float,
    instance_id: str,
    lease_seconds: int,
) -> CronJobFireRecord | None:
    fire_time = _as_utc(scheduled_fire_at)
    lease_until = datetime.now(UTC) + timedelta(seconds=lease_seconds)
    row = CronJobFireRow(
        fire_id=str(uuid.uuid4()),
        job_id=job_id,
        scheduled_fire_at=fire_time,
        status="claimed",
        claim_owner=instance_id,
        claim_token=str(uuid.uuid4()),
        lease_until=lease_until,
    )
    async with self._sf() as session:
        session.add(row)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return None
        await session.refresh(row)
    return self._fire_row_to_record(row)
```

```python
class CronSchedulerService:
    def __init__(self, repo: CronSchedulerRepository, run_launcher: RunLauncher, *, instance_id: str, poll_interval: float = 10.0, lease_seconds: int = 30) -> None:
        self._repo = repo
        self._run_launcher = run_launcher
        self._instance_id = instance_id
        self._poll_interval = poll_interval
        self._lease_seconds = lease_seconds

    async def dispatch_due_jobs(self, *, now: float | None = None, limit: int = 100) -> list[str]:
        launched: list[str] = []
        for job in await self._repo.list_due_jobs(now=now, limit=limit):
            fire = await self._repo.claim_fire(
                job.job_id,
                scheduled_fire_at=job.next_fire_at,
                instance_id=self._instance_id,
                lease_seconds=self._lease_seconds,
            )
            if fire is None:
                continue
            run = await self._run_launcher(job, fire)
            await self._repo.mark_fire_dispatched(job.job_id, fire.fire_id, run_id=run.run_id, fired_at=job.next_fire_at)
            launched.append(run.run_id)
        return launched
```

- [ ] **Step 4: Run service tests**

Run: `cd backend && uv run pytest tests/test_cron_scheduler_service.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/packages/harness/deerflow/persistence/scheduler/sql.py \
        backend/packages/harness/deerflow/runtime/scheduler/service.py \
        backend/packages/harness/deerflow/runtime/scheduler/__init__.py \
        backend/tests/test_cron_scheduler_service.py
git commit -m "feat: add cron fire claim and recovery flow"
```

## Task 4: Add Cron Run-Launch Idempotency

**Files:**
- Modify: `backend/app/gateway/services.py`
- Modify: `backend/packages/harness/deerflow/persistence/run/sql.py`
- Test: `backend/tests/test_cron_dispatch_idempotency.py`
- Test: `backend/tests/test_gateway_services.py`

- [ ] **Step 1: Write the failing idempotency test**

```python
async def test_start_cron_run_reuses_existing_run(session_factory):
    run_repo = RunRepository(session_factory)
    existing = await run_repo.put(
        "run-1",
        thread_id="thread-1",
        status="success",
        metadata={"scheduler": {"idempotency_key": "cron:job-1:1746500000"}},
    )

    reused = await find_existing_cron_run(run_repo, "thread-1", "cron:job-1:1746500000")
    assert reused["run_id"] == "run-1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_cron_dispatch_idempotency.py::test_start_cron_run_reuses_existing_run -v`

Expected: FAIL with `ImportError` or missing helper

- [ ] **Step 3: Implement run lookup and cron launch helper**

```python
async def find_run_by_scheduler_idempotency_key(self, thread_id: str, key: str) -> dict[str, Any] | None:
    stmt = (
        select(RunRow)
        .where(
            RunRow.thread_id == thread_id,
            RunRow.metadata_json["scheduler"]["idempotency_key"].as_string() == key,
            RunRow.status.in_(("pending", "running", "success")),
        )
        .order_by(RunRow.created_at.desc())
        .limit(1)
    )
    async with self._sf() as session:
        row = (await session.execute(stmt)).scalar_one_or_none()
    return self._row_to_dict(row) if row is not None else None
```

```python
async def start_cron_run_with_deps(
    request: RunLaunchRequest,
    *,
    thread_id: str,
    scheduler_job_id: str,
    scheduler_fire_id: str,
    scheduled_fire_at: float,
    bridge: StreamBridge,
    run_mgr: RunManager,
    checkpointer: Any,
    store: Any,
) -> RunRecord:
    idempotency_key = f"cron:{scheduler_job_id}:{int(scheduled_fire_at)}"
    existing = await _find_existing_cron_run(thread_id, idempotency_key)
    if existing is not None:
        return RunRecord(**existing)

    request.metadata = dict(request.metadata or {})
    request.metadata["scheduler"] = {
        "job_id": scheduler_job_id,
        "fire_id": scheduler_fire_id,
        "scheduled_fire_at": scheduled_fire_at,
        "idempotency_key": idempotency_key,
    }
    return await start_run_with_deps(request, thread_id, bridge=bridge, run_mgr=run_mgr, checkpointer=checkpointer, store=store)
```

- [ ] **Step 4: Run idempotency-focused tests**

Run: `cd backend && uv run pytest tests/test_cron_dispatch_idempotency.py tests/test_gateway_services.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/gateway/services.py \
        backend/packages/harness/deerflow/persistence/run/sql.py \
        backend/tests/test_cron_dispatch_idempotency.py \
        backend/tests/test_gateway_services.py
git commit -m "feat: add cron dispatch idempotency"
```

## Task 5: Add Gateway Cron Scheduler Wiring and Router

**Files:**
- Create: `backend/app/gateway/cron_scheduler.py`
- Create: `backend/app/gateway/routers/cron.py`
- Modify: `backend/app/gateway/app.py`
- Modify: `backend/app/gateway/routers/__init__.py`
- Test: `backend/tests/test_cron_router.py`
- Test: `backend/tests/test_gateway_lifespan_shutdown.py`

- [ ] **Step 1: Write the failing router test**

```python
async def test_create_cron_job_route(async_client):
    response = await async_client.post(
        "/api/cron/jobs",
        json={
            "thread_id": "thread-1",
            "assistant_id": "lead_agent",
            "cron": "0 9 * * *",
            "timezone": "Asia/Shanghai",
        },
    )
    assert response.status_code == 200
    assert response.json()["thread_id"] == "thread-1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_cron_router.py::test_create_cron_job_route -v`

Expected: FAIL with `404 Not Found`

- [ ] **Step 3: Implement gateway wiring**

```python
router = APIRouter(prefix="/api/cron/jobs", tags=["cron"])


@router.post("", response_model=CronJobRecord)
async def create_job(body: CronJobCreate, request: Request) -> CronJobRecord:
    scheduler = build_request_cron_scheduler(request)
    return await scheduler.create_job(body)


@router.post("/{job_id}/trigger", response_model=CronTriggerResponse)
async def trigger_job(job_id: str, request: Request) -> CronTriggerResponse:
    scheduler = build_request_cron_scheduler(request)
    run = await scheduler.trigger_job(job_id)
    return CronTriggerResponse(run_id=run.run_id, thread_id=run.thread_id, status=run.status.value)
```

```python
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    async with langgraph_runtime(app):
        await start_gateway_cron_scheduler(app)
        yield
        await stop_gateway_cron_scheduler(app)
```

- [ ] **Step 4: Run router and lifespan tests**

Run: `cd backend && uv run pytest tests/test_cron_router.py tests/test_gateway_lifespan_shutdown.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/gateway/cron_scheduler.py \
        backend/app/gateway/routers/cron.py \
        backend/app/gateway/app.py \
        backend/app/gateway/routers/__init__.py \
        backend/tests/test_cron_router.py \
        backend/tests/test_gateway_lifespan_shutdown.py
git commit -m "feat: add gateway cron router and lifecycle wiring"
```

## Task 6: Add Built-In Schedule Tools

**Files:**
- Create: `backend/packages/harness/deerflow/tools/builtins/schedule_tool.py`
- Modify: `backend/packages/harness/deerflow/tools/builtins/__init__.py`
- Test: `backend/tests/test_schedule_tool.py`

- [ ] **Step 1: Write the failing tool test**

```python
async def test_pause_schedule_tool_calls_scheduler_service(fake_runtime):
    result = await pause_schedule_tool.ainvoke(
        {"job_id": "job-1"},
        config={"context": {"app_config": fake_runtime.app_config}},
    )
    assert "paused" in result.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_schedule_tool.py::test_pause_schedule_tool_calls_scheduler_service -v`

Expected: FAIL with `ModuleNotFoundError` or missing tool

- [ ] **Step 3: Implement structured schedule tools**

```python
@tool("pause_schedule", parse_docstring=True)
async def pause_schedule_tool(job_id: str, runtime: ToolRuntime | None = None) -> str:
    """Pause an existing schedule by job id."""
    scheduler = _resolve_scheduler_service(runtime)
    record = await scheduler.pause_job(job_id)
    return f"Schedule {record.job_id} paused."


@tool("create_schedule", parse_docstring=True)
async def create_schedule_tool(
    thread_id: str,
    cron: str,
    assistant_id: str | None = None,
    input: dict[str, Any] | None = None,
    runtime: ToolRuntime | None = None,
) -> str:
    scheduler = _resolve_scheduler_service(runtime)
    record = await scheduler.create_job(
        CronJobCreate(
            thread_id=thread_id,
            assistant_id=assistant_id,
            cron=cron,
            timezone=_system_timezone(),
            input=input,
        )
    )
    return f"Schedule {record.job_id} created for thread {record.thread_id}."
```

- [ ] **Step 4: Run tool tests**

Run: `cd backend && uv run pytest tests/test_schedule_tool.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/packages/harness/deerflow/tools/builtins/schedule_tool.py \
        backend/packages/harness/deerflow/tools/builtins/__init__.py \
        backend/tests/test_schedule_tool.py
git commit -m "feat: add built-in schedule management tools"
```

## Task 7: Add Concurrency and Recovery Integration Tests

**Files:**
- Modify: `backend/tests/test_cron_scheduler_service.py`
- Modify: `backend/tests/test_cron_dispatch_idempotency.py`

- [ ] **Step 1: Write the failing concurrency test**

```python
async def test_two_workers_dispatch_same_due_job_once(session_factory):
    repo = CronSchedulerRepository(session_factory)
    launcher = AsyncMock(side_effect=[SimpleNamespace(run_id="run-1"), SimpleNamespace(run_id="run-2")])
    worker_a = CronSchedulerService(repo, launcher, instance_id="worker-a")
    worker_b = CronSchedulerService(repo, launcher, instance_id="worker-b")

    job = await repo.create_job(
        CronJobCreate(
            thread_id="thread-1",
            assistant_id="lead_agent",
            cron="*/5 * * * *",
            timezone="Asia/Shanghai",
        ),
        now=1_746_500_000,
    )

    await asyncio.gather(
        worker_a.dispatch_due_jobs(now=job.next_fire_at),
        worker_b.dispatch_due_jobs(now=job.next_fire_at),
    )

    assert launcher.await_count == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_cron_scheduler_service.py::test_two_workers_dispatch_same_due_job_once -v`

Expected: FAIL with launch count `2` or missing recovery behavior

- [ ] **Step 3: Tighten claim and recovery behavior**

```python
async def dispatch_due_jobs(self, *, now: float | None = None, limit: int = 100) -> list[str]:
    launched: list[str] = []
    for job in await self._repo.list_due_jobs(now=now, limit=limit):
        fire = await self._repo.claim_fire(
            job.job_id,
            scheduled_fire_at=job.next_fire_at,
            instance_id=self._instance_id,
            lease_seconds=self._lease_seconds,
        )
        if fire is None:
            fire = await self._repo.recover_expired_fire(
                job.job_id,
                scheduled_fire_at=job.next_fire_at,
                instance_id=self._instance_id,
                lease_seconds=self._lease_seconds,
            )
        if fire is None:
            continue
        run = await self._run_launcher(job, fire)
        await self._repo.mark_fire_dispatched(job.job_id, fire.fire_id, run_id=run.run_id, fired_at=job.next_fire_at)
        launched.append(run.run_id)
    return launched
```

- [ ] **Step 4: Run focused cron test suite**

Run: `cd backend && uv run pytest tests/test_cron_scheduler_schemas.py tests/test_cron_scheduler_repository.py tests/test_cron_scheduler_service.py tests/test_cron_router.py tests/test_schedule_tool.py tests/test_cron_dispatch_idempotency.py tests/test_gateway_services.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/tests/test_cron_scheduler_service.py \
        backend/tests/test_cron_dispatch_idempotency.py \
        backend/packages/harness/deerflow/persistence/scheduler/sql.py \
        backend/packages/harness/deerflow/runtime/scheduler/service.py
git commit -m "test: cover cron scheduler concurrency and recovery"
```

## Task 8: Run Full Verification and Update Developer Docs

**Files:**
- Modify: `backend/CLAUDE.md`
- Modify: `README.md`

- [ ] **Step 1: Write the failing documentation assertion**

```python
def test_cron_docs_strings_exist():
    readme = Path("README.md").read_text()
    assert "/api/cron/jobs" in readme
    assert "multi-instance" in readme
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_cron_router.py::test_cron_docs_strings_exist -v`

Expected: FAIL because docs are missing

- [ ] **Step 3: Update docs and operator notes**

```md
### Built-in Cron Scheduler

- SQL-backed cron jobs and fire records
- Multi-instance-safe dispatch claim model
- Existing run lifecycle reuse
- Manual trigger support
- Postgres required for production multi-instance deployments
```

```md
## Cron Scheduler Notes

- Every Gateway instance may run the scheduler loop
- Duplicate suppression relies on SQL fire claiming and run idempotency
- Redis is optional future acceleration, not a current requirement
```

- [ ] **Step 4: Run full verification**

Run: `cd backend && uv run pytest tests/test_cron_scheduler_schemas.py tests/test_cron_scheduler_repository.py tests/test_cron_scheduler_service.py tests/test_cron_router.py tests/test_schedule_tool.py tests/test_cron_dispatch_idempotency.py tests/test_gateway_services.py tests/test_gateway_lifespan_shutdown.py -v`

Expected: PASS

Run: `cd backend && make lint`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add README.md backend/CLAUDE.md
git commit -m "docs: document cron scheduler architecture"
```

## Self-Review

### Spec coverage

- User-managed CRUD and manual trigger are covered in Tasks 5 and 6.
- SQL-backed `cron_jobs` and `cron_job_fires` are covered in Tasks 1 and 2.
- Database claim uniqueness and lease recovery are covered in Tasks 3 and 7.
- Run-launch idempotency is covered in Task 4.
- Gateway integration is covered in Task 5.
- Postgres-first, Redis-later architecture is reflected in Tasks 2, 3, and 8.

### Placeholder scan

- No `TODO`, `TBD`, or "implement later" placeholders remain.
- Every coding step includes concrete file targets, commands, and code skeletons.
- Every verification step names exact pytest commands and expected outcomes.

### Type consistency

- `CronJobCreate`, `CronJobRecord`, `CronSchedulerRepository`, and `CronSchedulerService` are introduced before later tasks reference them.
- The scheduler idempotency key uses one stable format throughout the plan: `cron:{job_id}:{scheduled_fire_at}`.
- Router, tool, and service tasks consistently reference the same CRUD surface and scheduler service layer.
