"""Focused Goal 4 multi-connection checks against a disposable MySQL 8.0.24 DB.

Set ``TEST_MYSQL_URI`` to a disposable database.  The tests create only their
required application tables; Gateway's production path still performs no DDL.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import Column, MetaData, String, Table, delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from deerflow.persistence.models.run_event import RunEventRow
from deerflow.persistence.mysql_errors import MYSQL_DUPLICATE_KEY, is_mysql_deadlock, mysql_duplicate_key_name, mysql_error_code
from deerflow.persistence.projects import ProjectNotAssignableError, ProjectRepository
from deerflow.persistence.projects.model import ProjectRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.run.sql import RunRepository
from deerflow.persistence.scheduled_task_runs.model import ScheduledTaskRunRow
from deerflow.persistence.scheduled_task_runs.sql import ScheduledTaskRunRepository
from deerflow.persistence.scheduled_tasks.model import ScheduledTaskRow
from deerflow.persistence.scheduled_tasks.sql import ScheduledTaskRepository
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.persistence.thread_meta.sql import ThreadMetaRepository
from deerflow.runtime.events.store.db import DbRunEventStore
from deerflow.runtime.runs.manager import ConflictError, RunManager, _is_active_run_conflict, _is_unique_violation

pytestmark = pytest.mark.integration


def _uri() -> str:
    uri = os.environ.get("TEST_MYSQL_URI")
    if not uri:
        pytest.skip("TEST_MYSQL_URI is not set")
    return uri.replace("mysql://", "mysql+asyncmy://", 1)


def test_mysql_deadlock_is_not_a_unique_violation() -> None:
    deadlock = type("Deadlock", (), {"args": (1213, "Deadlock found when trying to get lock")})()

    assert is_mysql_deadlock(deadlock)
    assert mysql_duplicate_key_name(deadlock) is None


async def _create_tables(engine, *tables) -> None:
    async with engine.begin() as connection:
        for table in tables:
            await connection.run_sync(lambda sync, table=table: table.create(sync, checkfirst=True))


async def _delete_prefix(engine, *tables, prefix: str) -> None:
    async with engine.begin() as connection:
        for table, column in tables:
            await connection.execute(delete(table).where(column.like(f"{prefix}%")))


@pytest.mark.anyio
async def test_mysql_run_events_anchor_serializes_new_thread_writers_and_preserves_metadata() -> None:
    engine = create_async_engine(_uri(), isolation_level="READ COMMITTED", pool_size=2, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    prefix = f"g4-events-{uuid4().hex}-"
    thread_id = f"{prefix}new"
    try:
        await _create_tables(engine, ThreadMetaRow.__table__, RunEventRow.__table__)
        async with engine.connect() as connection:
            assert (await connection.execute(text("SELECT @@transaction_isolation"))).scalar_one() == "READ-COMMITTED"

        # Both workers reach the durable lock protocol together.  The anchor
        # row then serializes their max(seq)+1 allocation across connections.
        barrier = asyncio.Barrier(2)

        class BarrierStore(DbRunEventStore):
            async def _max_seq_for_thread(self, session, thread_id):  # type: ignore[override]
                await barrier.wait()
                return await super()._max_seq_for_thread(session, thread_id)

        stores = [BarrierStore(sessions), BarrierStore(sessions)]
        written = await asyncio.gather(
            stores[0].put(thread_id=thread_id, run_id=f"{prefix}run-a", event_type="message", category="message", content="a"),
            stores[1].put(thread_id=thread_id, run_id=f"{prefix}run-b", event_type="message", category="message", content="b"),
        )
        assert sorted(event["seq"] for event in written) == [1, 2]
        async with sessions() as session:
            rows = list((await session.execute(select(RunEventRow.seq).where(RunEventRow.thread_id == thread_id).order_by(RunEventRow.seq))).scalars())
        assert rows == [1, 2]

        # The anchor's upsert must not mutate an established metadata row.
        existing_thread = f"{prefix}existing"
        async with sessions.begin() as session:
            session.add(ThreadMetaRow(thread_id=existing_thread, assistant_id="assistant", user_id="owner", status="archived", metadata_json={"pinned": True}))
        await DbRunEventStore(sessions).put(thread_id=existing_thread, run_id=f"{prefix}run-c", event_type="message", category="message")
        async with sessions() as session:
            preserved = await session.get(ThreadMetaRow, existing_thread)
        assert preserved is not None
        assert (preserved.assistant_id, preserved.user_id, preserved.status, preserved.metadata_json) == ("assistant", "owner", "archived", {"pinned": True})
    finally:
        await _delete_prefix(
            engine,
            (RunEventRow.__table__, RunEventRow.thread_id),
            (ThreadMetaRow.__table__, ThreadMetaRow.thread_id),
            prefix=prefix,
        )
        await engine.dispose()


@pytest.mark.anyio
async def test_mysql_scheduler_budget_and_skip_locked_are_cross_worker_safe() -> None:
    engine = create_async_engine(_uri(), isolation_level="READ COMMITTED", pool_size=3, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    prefix = f"g4-scheduler-{uuid4().hex}-"
    metadata = MetaData()
    alembic_version = Table("alembic_version", metadata, Column("version_num", String(64), primary_key=True))
    now = datetime.now(UTC)
    try:
        await _create_tables(engine, ScheduledTaskRow.__table__, ScheduledTaskRunRow.__table__, alembic_version)
        async with engine.begin() as connection:
            await connection.execute(text("INSERT IGNORE INTO alembic_version (version_num) VALUES ('0001_mysql_baseline')"))
            queue_index = await connection.execute(
                text("SELECT COLUMN_NAME FROM information_schema.statistics WHERE table_schema = DATABASE() AND table_name = 'scheduled_task_runs' AND index_name = 'idx_scheduled_task_runs_status_created' ORDER BY seq_in_index")
            )
            assert list(queue_index.scalars()) == ["status", "attempt_count", "created_at", "id"]
            await connection.execute(
                ScheduledTaskRunRow.__table__.insert(),
                [
                    {"id": f"{prefix}queued-a", "task_id": f"{prefix}task-a", "thread_id": f"{prefix}thread-a", "scheduled_for": now, "trigger": "cron", "status": "queued", "created_at": now},
                    {"id": f"{prefix}queued-b", "task_id": f"{prefix}task-b", "thread_id": f"{prefix}thread-b", "scheduled_for": now, "trigger": "cron", "status": "queued", "created_at": now},
                ],
            )

        repo_a, repo_b = ScheduledTaskRunRepository(sessions), ScheduledTaskRunRepository(sessions)
        claims = await asyncio.gather(
            repo_a.claim_queued_run(f"{prefix}queued-a", lease_owner="worker-a", now=now, lease_seconds=30, global_max_concurrent_runs=1),
            repo_b.claim_queued_run(f"{prefix}queued-b", lease_owner="worker-b", now=now, lease_seconds=30, global_max_concurrent_runs=1),
        )
        assert sum(claim is not None for claim in claims) == 1

        # A worker holding the first due task cannot make another scheduler
        # wait: MySQL's SKIP LOCKED must claim the independent due row.
        due_a, due_b = f"{prefix}due-a", f"{prefix}due-b"
        async with sessions.begin() as session:
            session.add_all(
                [
                    ScheduledTaskRow(id=due_a, user_id="user", title="a", prompt="p", schedule_type="cron", schedule_spec={}, timezone="UTC", next_run_at=now),
                    ScheduledTaskRow(id=due_b, user_id="user", title="b", prompt="p", schedule_type="cron", schedule_spec={}, timezone="UTC", next_run_at=now),
                ]
            )
        locked = asyncio.Event()
        release = asyncio.Event()

        async def hold_first_due_row() -> None:
            async with sessions() as session:
                async with session.begin():
                    await session.execute(select(ScheduledTaskRow).where(ScheduledTaskRow.id == due_a).with_for_update())
                    locked.set()
                    await release.wait()

        holder = asyncio.create_task(hold_first_due_row())
        await locked.wait()
        try:
            claimed_due = await asyncio.wait_for(ScheduledTaskRepository(sessions).claim_due_tasks(now=now, lease_owner="worker-c", lease_seconds=30, limit=2), timeout=3)
        finally:
            release.set()
            await holder
        assert [row["id"] for row in claimed_due] == [due_b]
    finally:
        await _delete_prefix(
            engine,
            (ScheduledTaskRunRow.__table__, ScheduledTaskRunRow.id),
            (ScheduledTaskRow.__table__, ScheduledTaskRow.id),
            prefix=prefix,
        )
        await engine.dispose()


@pytest.mark.anyio
async def test_mysql_run_lease_compare_and_set_fences_competing_workers() -> None:
    engine = create_async_engine(_uri(), isolation_level="READ COMMITTED", pool_size=2, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    prefix = f"g4-runs-{uuid4().hex}-"
    run_id = f"{prefix}run"
    try:
        await _create_tables(engine, RunRow.__table__)
        repo_a, repo_b = RunRepository(sessions), RunRepository(sessions)
        expired = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        await repo_a.put(run_id, thread_id=f"{prefix}thread", user_id="user", owner_worker_id="worker-a", lease_expires_at=expired)

        starts = await asyncio.gather(repo_a.start_run(run_id), repo_b.start_run(run_id))
        assert starts.count(True) == 1
        assert await repo_a.update_lease(run_id, owner_worker_id="worker-a", lease_expires_at=(datetime.now(UTC) + timedelta(minutes=1)).isoformat())
        assert not await repo_b.update_lease(run_id, owner_worker_id="worker-b", lease_expires_at=(datetime.now(UTC) + timedelta(minutes=1)).isoformat())

        actions = await asyncio.gather(repo_a.request_cancel(run_id, action="interrupt"), repo_b.request_cancel(run_id, action="rollback"))
        assert actions[0] == actions[1] and actions[0] in {"interrupt", "rollback"}
        assert not (await repo_a.finalize_if_not_cancelled(run_id, status="success")).finalized

        assert not await repo_b.claim_for_takeover(run_id, grace_seconds=0, error="not expired")
        async with sessions.begin() as session:
            await session.execute(text("UPDATE runs SET lease_expires_at = UTC_TIMESTAMP(6) - INTERVAL 1 SECOND WHERE run_id = :run_id"), {"run_id": run_id})
        takeovers = await asyncio.gather(
            repo_a.claim_for_takeover(run_id, grace_seconds=0, error="owner expired"),
            repo_b.claim_for_takeover(run_id, grace_seconds=0, error="owner expired"),
        )
        assert takeovers.count(True) == 1
    finally:
        await _delete_prefix(engine, (RunRow.__table__, RunRow.run_id), prefix=prefix)
        await engine.dispose()


# ---------------------------------------------------------------------------
# R2 blocker fixes — real MySQL 8.0.24 interleavings
# ---------------------------------------------------------------------------

# MySQL has no partial unique index, so the ``runs`` single-active-run guard is
# a stored generated column plus a unique key carrying the ORM index name.
_MYSQL_ACTIVE_RUN_DDL = """
    ALTER TABLE runs
      ADD COLUMN active_thread_id VARCHAR(64) GENERATED ALWAYS AS
        (IF(status IN ('pending','running'), thread_id, NULL)) STORED,
      ADD UNIQUE KEY uq_runs_thread_active (active_thread_id)
"""

# The completion hook's terminal write, reproduced verbatim.
_MYSQL_COMPLETE_OCCURRENCE = text(
    """
    UPDATE scheduled_task_runs
       SET status = 'success',
           run_id = 'g4-r2-run',
           error = NULL,
           finished_at = UTC_TIMESTAMP(6),
           lease_owner = NULL,
           lease_expires_at = NULL
     WHERE id = :record
    """
)


@asynccontextmanager
async def _disposable_database(*, pool_size: int = 4):
    """Yield a fresh disposable MySQL 8.0.24 database and its READ COMMITTED pool."""
    from sqlalchemy.engine import make_url

    from deerflow.config.database_config import DatabaseConfig

    config = DatabaseConfig(backend="mysql", mysql_url=_uri())
    admin = create_async_engine(config.app_sqlalchemy_url, pool_size=1, max_overflow=0)
    name = f"g4_r2_{uuid4().hex}"
    engine = None
    try:
        async with admin.begin() as connection:
            assert str((await connection.execute(text("SELECT VERSION()"))).scalar_one()).startswith("8.0.24")
            await connection.execute(text(f"CREATE DATABASE `{name}`"))
        engine = create_async_engine(
            make_url(config.app_sqlalchemy_url).set(database=name),
            isolation_level="READ COMMITTED",
            pool_size=pool_size,
            max_overflow=0,
        )
        yield engine, async_sessionmaker(engine, expire_on_commit=False)
    finally:
        if engine is not None:
            await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f"DROP DATABASE IF EXISTS `{name}`"))
        await admin.dispose()


class _Rendezvous:
    """Release every participant together, tolerating extra arrivals."""

    def __init__(self, parties: int) -> None:
        self._parties = parties
        self._arrived = 0
        self._release = asyncio.Event()

    async def wait(self) -> None:
        self._arrived += 1
        if self._arrived >= self._parties:
            self._release.set()
        await asyncio.wait_for(self._release.wait(), timeout=15)


@pytest.mark.anyio
async def test_mysql_scheduler_terminal_status_survives_a_delayed_running_write() -> None:
    """The launch-path ``running`` write must not resurrect a finished occurrence.

    Completion holds the occurrence row and has not committed its terminal state
    yet.  The late launcher's write therefore blocks on that row lock; once the
    terminal state lands, the conditional update must refuse to overwrite it and
    only backfill what completion could not know.
    """
    async with _disposable_database() as (engine, sessions):
        await _create_tables(engine, ScheduledTaskRunRow.__table__)
        now = datetime.now(UTC)
        record = f"g4-r2-occurrence-{uuid4().hex}"
        async with sessions.begin() as session:
            session.add(
                ScheduledTaskRunRow(
                    id=record,
                    task_id=f"g4-r2-task-{record}",
                    thread_id=f"g4-r2-thread-{record}",
                    scheduled_for=now,
                    trigger="scheduled",
                    status="launching",
                    lease_owner="worker-a",
                    lease_expires_at=now + timedelta(seconds=120),
                    created_at=now,
                )
            )
        repo = ScheduledTaskRunRepository(sessions)
        started_at = now + timedelta(seconds=1)

        completion = await engine.connect()
        try:
            await completion.execute(text("SELECT id FROM scheduled_task_runs WHERE id = :record FOR UPDATE"), {"record": record})
            late = asyncio.create_task(
                repo.update_status(
                    record,
                    status="running",
                    run_id="g4-r2-run",
                    started_at=started_at,
                    protect_terminal=True,
                    expected_lease_owner="worker-a",
                )
            )
            await asyncio.sleep(0.5)
            assert not late.done(), "the delayed launch write must wait for the completion row lock"
            await completion.execute(_MYSQL_COMPLETE_OCCURRENCE, {"record": record})
            await completion.commit()
            assert await asyncio.wait_for(late, timeout=10) is True
        finally:
            await completion.close()

        async with sessions() as session:
            row = await session.get(ScheduledTaskRunRow, record)
        assert row is not None
        assert row.status == "success"
        assert row.run_id == "g4-r2-run"
        assert row.lease_owner is None
        assert row.lease_expires_at is None
        assert row.started_at is not None


@pytest.mark.anyio
async def test_mysql_two_worker_admission_loser_keeps_the_overlap_contract() -> None:
    """The admission loser must surface the existing 409 overlap contract.

    asyncmy raises ``(1062, "Duplicate entry ... for key 'runs.uq_runs_thread_active'")``
    without an ``errno`` attribute, so the pre-existing generic unique-violation
    probe cannot see MySQL's shape.  Only the active-run key may be classified as
    an overlap; a primary-key collision must keep its own semantics.
    """
    async with _disposable_database() as (engine, sessions):
        await _create_tables(engine, RunRow.__table__)
        async with engine.begin() as connection:
            await connection.execute(text(_MYSQL_ACTIVE_RUN_DDL))

        captured: dict[str, BaseException] = {}
        rendezvous = _Rendezvous(2)

        class RendezvousRunRepository(RunRepository):
            async def create_thread_operation_atomic(self, *args, **kwargs):
                await rendezvous.wait()
                try:
                    return await super().create_thread_operation_atomic(*args, **kwargs)
                except BaseException as exc:  # noqa: BLE001 - re-raised unchanged
                    captured.setdefault("error", exc)
                    raise

        store = RendezvousRunRepository(sessions)
        thread_id = f"g4-r2-thread-{uuid4().hex}"
        outcomes = await asyncio.gather(
            RunManager(store=store, worker_id="worker-a").create_or_reject(thread_id, user_id="user"),
            RunManager(store=store, worker_id="worker-b").create_or_reject(thread_id, user_id="user"),
            return_exceptions=True,
        )
        winners = [outcome for outcome in outcomes if not isinstance(outcome, BaseException)]
        losers = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
        assert len(winners) == 1 and len(losers) == 1
        assert isinstance(losers[0], ConflictError)
        assert "already has an active run" in str(losers[0])

        raw = captured["error"]
        assert mysql_error_code(raw) == MYSQL_DUPLICATE_KEY
        assert mysql_duplicate_key_name(raw).rsplit(".", 1)[-1] == "uq_runs_thread_active"
        # The classifier keys off that literal, so the ORM index name and the
        # MySQL baseline's unique key must both keep it (design §8.4).
        assert "uq_runs_thread_active" in {index.name for index in RunRow.__table__.indexes}
        # The generic probe cannot see MySQL's 1062 shape ...
        assert _is_unique_violation(raw) is False
        # ... so the active-run key check is the load-bearing discriminator.
        assert _is_active_run_conflict(raw) is True

        # The Scheduler keeps its existing overlap handling for this exception.
        from app.scheduler.service import ScheduledTaskService

        assert ScheduledTaskService._is_overlap_conflict(losers[0]) is True

        # A 1062 on another unique key is not an active-run conflict.
        plain = RunRepository(sessions)
        await plain.create_thread_operation_atomic("g4-r2-pk", thread_id=f"{thread_id}-pk", owner_worker_id="worker-a", lease_expires_at=None)
        with pytest.raises(Exception) as primary:
            await plain.create_thread_operation_atomic("g4-r2-pk", thread_id=f"{thread_id}-pk", owner_worker_id="worker-a", lease_expires_at=None)
        assert mysql_error_code(primary.value) == MYSQL_DUPLICATE_KEY
        assert mysql_duplicate_key_name(primary.value).rsplit(".", 1)[-1] == "PRIMARY"
        assert _is_active_run_conflict(primary.value) is False


@pytest.mark.anyio
async def test_mysql_scheduler_requeues_an_overlap_conflict_instead_of_failing_the_occurrence() -> None:
    """A real admission overlap must requeue the occurrence, not fail it."""
    async with _disposable_database() as (engine, sessions):
        metadata = MetaData()
        alembic_version = Table("alembic_version", metadata, Column("version_num", String(64), primary_key=True))
        await _create_tables(engine, ScheduledTaskRow.__table__, ScheduledTaskRunRow.__table__, RunRow.__table__, alembic_version)
        async with engine.begin() as connection:
            await connection.execute(text("INSERT IGNORE INTO alembic_version (version_num) VALUES ('0001_mysql_baseline')"))
            await connection.execute(text(_MYSQL_ACTIVE_RUN_DDL))

        from app.scheduler.service import ScheduledTaskService

        now = datetime.now(UTC)
        suffix = uuid4().hex
        task_id = f"g4-r2-task-{suffix}"
        thread_id = f"g4-r2-thread-{suffix}"
        occurrence_id = f"g4-r2-occurrence-{suffix}"
        tasks = ScheduledTaskRepository(sessions)
        occurrences = ScheduledTaskRunRepository(sessions)
        await tasks.create(
            task_id=task_id,
            user_id="user",
            thread_id=thread_id,
            context_mode="reuse_thread",
            assistant_id=None,
            title="r2",
            prompt="p",
            schedule_type="cron",
            schedule_spec={"cron": "0 9 * * *"},
            timezone="UTC",
            next_run_at=now,
        )
        run_manager = RunManager(store=RunRepository(sessions), worker_id="worker-a")
        await run_manager.create_or_reject(thread_id, user_id="user")
        await occurrences.create(
            run_record_id=occurrence_id,
            task_id=task_id,
            thread_id=thread_id,
            scheduled_for=now,
            trigger="manual",
            status="queued",
        )

        async def launch_run(**kwargs):
            record = await run_manager.create_or_reject(kwargs["thread_id"], user_id="user")
            return {"run_id": record.run_id, "thread_id": record.thread_id}

        service = ScheduledTaskService(
            task_repo=tasks,
            task_run_repo=occurrences,
            launch_run=launch_run,
            poll_interval_seconds=5,
            lease_seconds=120,
            max_concurrent_runs=3,
        )
        result = await service._launch_queued_occurrence(
            {
                "id": task_id,
                "prompt": "p",
                "assistant_id": None,
                "user_id": "user",
                "schedule_type": "cron",
                "schedule_spec": {"cron": "0 9 * * *"},
                "timezone": "UTC",
                "thread_id": thread_id,
                "status": "enabled",
            },
            {"id": occurrence_id, "task_id": task_id, "thread_id": thread_id, "trigger": "manual"},
            now=now,
        )

        assert result["outcome"] == "queued"
        row = (await occurrences.list_by_task(task_id))[0]
        assert row["status"] == "queued"
        assert row["finished_at"] is None
        assert "already has an active run" in (row["error"] or "")


@pytest.mark.anyio
async def test_mysql_event_anchor_and_metadata_create_interleavings() -> None:
    """An event-path anchor row must not break, or be clobbered by, metadata create."""
    async with _disposable_database() as (engine, sessions):
        await _create_tables(engine, ProjectRow.__table__, ThreadMetaRow.__table__, RunEventRow.__table__)
        store = DbRunEventStore(sessions)
        repo = ThreadMetaRepository(sessions)
        prefix = f"g4-r2-anchor-{uuid4().hex}-"

        # (a) The event path creates the empty anchor first; the normal metadata
        # create must promote it instead of failing on the primary key.
        anchored_first = f"{prefix}anchored-first"
        await store.put(thread_id=anchored_first, run_id=f"{prefix}run-a", event_type="message", category="message", content="a")
        created = await repo.create(anchored_first, assistant_id="assistant", user_id="owner", display_name="name", metadata={"pinned": True})
        assert created["thread_id"] == anchored_first
        assert created["assistant_id"] == "assistant"
        assert created["metadata"] == {"pinned": True}
        fetched = await repo.get(anchored_first, user_id="owner")
        assert fetched is not None and fetched["display_name"] == "name"
        await store.put(thread_id=anchored_first, run_id=f"{prefix}run-a", event_type="message", category="message", content="b")
        async with sessions() as session:
            seqs = list((await session.execute(select(RunEventRow.seq).where(RunEventRow.thread_id == anchored_first).order_by(RunEventRow.seq))).scalars())
        assert seqs == [1, 2]

        # (b) Metadata create wins first; the anchor upsert must be a no-op.
        created_first = f"{prefix}created-first"
        await repo.create(created_first, assistant_id="assistant", user_id="owner", metadata={"pinned": True})
        await store.put(thread_id=created_first, run_id=f"{prefix}run-b", event_type="message", category="message", content="a")
        preserved = await repo.get(created_first, user_id="owner")
        assert preserved is not None
        assert preserved["assistant_id"] == "assistant"
        assert preserved["metadata"] == {"pinned": True}

        # (c) Both paths reach their insert together for a brand-new thread.
        concurrent_threads = [f"{prefix}concurrent-{index}" for index in range(4)]
        rendezvous = _Rendezvous(2 * len(concurrent_threads))

        class RendezvousThreadMetaRepository(ThreadMetaRepository):
            async def create(self, thread_id, **kwargs):
                await rendezvous.wait()
                return await super().create(thread_id, **kwargs)

        class RendezvousRunEventStore(DbRunEventStore):
            async def _max_seq_for_thread(self, session, thread_id):  # type: ignore[override]
                await rendezvous.wait()
                return await super()._max_seq_for_thread(session, thread_id)

        concurrent_repo = RendezvousThreadMetaRepository(sessions)
        concurrent_store = RendezvousRunEventStore(sessions)
        results = await asyncio.gather(
            *[
                call
                for index, thread_id in enumerate(concurrent_threads)
                for call in (
                    concurrent_repo.create(thread_id, assistant_id="assistant", user_id="owner", metadata={"index": index}),
                    concurrent_store.put(thread_id=thread_id, run_id=f"{prefix}run-c{index}", event_type="message", category="message", content=str(index)),
                )
            ]
        )
        assert len(results) == 2 * len(concurrent_threads)
        for index, thread_id in enumerate(concurrent_threads):
            stored = await concurrent_repo.get(thread_id, user_id="owner")
            assert stored is not None
            assert stored["assistant_id"] == "assistant"
            assert stored["metadata"] == {"index": index}
        async with sessions() as session:
            seq_rows = list((await session.execute(select(RunEventRow.thread_id, RunEventRow.seq).where(RunEventRow.thread_id.in_(concurrent_threads)))).all())
        assert sorted(seq_rows, key=lambda row: (row[0], row[1])) == [(thread_id, 1) for thread_id in sorted(concurrent_threads)]

        # A duplicate create against a real metadata row keeps its contract.
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            await repo.create(created_first, assistant_id="other", user_id="owner")

        # (d) The duplicate-anchor INSERT rollback releases an earlier project
        # lock. If deletion wins that exact gap, the retry must reacquire and
        # reject the assignment instead of leaving a dangling project_id.
        projects = ProjectRepository(sessions)
        project = await projects.create(name="anchor-race", user_id="owner")
        project_thread = f"{prefix}project-delete"
        await store.put(thread_id=project_thread, run_id=f"{prefix}run-d", event_type="message", category="message", content="d")

        class DeleteBeforeRetryRepository(ThreadMetaRepository):
            def __init__(self, session_factory):
                super().__init__(session_factory)
                self.project_checks = 0

            async def _lock_assignable_project(self, session, project_id, resolved_user_id):
                self.project_checks += 1
                if self.project_checks == 2:
                    assert await projects.delete(project_id, user_id=resolved_user_id) is True
                return await super()._lock_assignable_project(session, project_id, resolved_user_id)

        racing_repo = DeleteBeforeRetryRepository(sessions)
        with pytest.raises(ProjectNotAssignableError):
            await racing_repo.create(project_thread, assistant_id="assistant", user_id="owner", project_id=project["id"])
        assert racing_repo.project_checks == 2
        preserved_anchor = await repo.get(project_thread, user_id=None)
        assert preserved_anchor is not None
        assert preserved_anchor["user_id"] is None
        assert preserved_anchor["metadata"].get("deerflow_project_id") is None
