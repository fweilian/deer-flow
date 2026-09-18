"""Focused Goal 4 multi-connection checks against a disposable MySQL 8.0.24 DB.

Set ``TEST_MYSQL_URI`` to a disposable database.  The tests create only their
required application tables; Gateway's production path still performs no DDL.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import Column, MetaData, String, Table, delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from deerflow.persistence.models.run_event import RunEventRow
from deerflow.persistence.mysql_errors import is_mysql_deadlock, mysql_duplicate_key_name
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.run.sql import RunRepository
from deerflow.persistence.scheduled_task_runs.model import ScheduledTaskRunRow
from deerflow.persistence.scheduled_task_runs.sql import ScheduledTaskRunRepository
from deerflow.persistence.scheduled_tasks.model import ScheduledTaskRow
from deerflow.persistence.scheduled_tasks.sql import ScheduledTaskRepository
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.runtime.events.store.db import DbRunEventStore

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
