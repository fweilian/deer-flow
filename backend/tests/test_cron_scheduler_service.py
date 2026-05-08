from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import pytest
from sqlalchemy import select

from deerflow.persistence.engine import close_engine, get_session_factory, init_engine
from deerflow.persistence.scheduler.model import CronJobFireRow, CronJobRow
from deerflow.persistence.scheduler.sql import CronSchedulerRepository
from deerflow.runtime.scheduler.schemas import CronJobCreate
from deerflow.runtime.scheduler.service import CronSchedulerService


@pytest.fixture
async def session_factory(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


@pytest.mark.anyio
async def test_claim_due_fire_is_unique(session_factory):
    repo = CronSchedulerRepository(session_factory)
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
    assert first.claim_token is not None
    assert second is None


@pytest.mark.anyio
async def test_dispatch_due_jobs_launches_and_marks_fire(session_factory):
    repo = CronSchedulerRepository(session_factory)
    launcher = AsyncMock(return_value=type("Run", (), {"run_id": "run-1"})())
    service = CronSchedulerService(repo, run_launcher=launcher, instance_id="worker-a")
    job = await repo.create_job(
        CronJobCreate(
            thread_id="thread-1",
            assistant_id="lead_agent",
            cron="*/5 * * * *",
            timezone="Asia/Shanghai",
        ),
        now=1_746_500_000,
    )

    launched = await service.dispatch_due_jobs(now=job.next_fire_at)

    assert launched == ["run-1"]
    launcher.assert_awaited_once()

    async with session_factory() as session:
        fire = (
            await session.execute(
                select(CronJobFireRow).where(
                    CronJobFireRow.job_id == job.job_id,
                    CronJobFireRow.scheduled_fire_at == datetime.fromtimestamp(job.next_fire_at, UTC),
                )
            )
        ).scalar_one()
        refreshed_job = (await session.execute(select(CronJobRow).where(CronJobRow.job_id == job.job_id))).scalar_one()

    assert fire.status == "dispatched"
    assert fire.run_id == "run-1"
    assert refreshed_job.last_run_id == "run-1"
    assert refreshed_job.last_fire_at is not None
    assert refreshed_job.next_fire_at is not None
    assert refreshed_job.next_fire_at > refreshed_job.last_fire_at


@pytest.mark.anyio
async def test_dispatch_due_jobs_recovers_expired_fire_lease(session_factory):
    repo = CronSchedulerRepository(session_factory)
    launcher = AsyncMock(return_value=type("Run", (), {"run_id": "run-1"})())
    service = CronSchedulerService(repo, run_launcher=launcher, instance_id="worker-b", lease_seconds=30)
    job = await repo.create_job(
        CronJobCreate(
            thread_id="thread-1",
            assistant_id="lead_agent",
            cron="*/5 * * * *",
            timezone="Asia/Shanghai",
        ),
        now=1_746_500_000,
    )

    fire = await repo.claim_fire(job.job_id, scheduled_fire_at=job.next_fire_at, instance_id="worker-a", lease_seconds=30)
    assert fire is not None

    async with session_factory() as session:
        stale_fire = (await session.execute(select(CronJobFireRow).where(CronJobFireRow.fire_id == fire.fire_id))).scalar_one()
        stale_fire.lease_until = datetime.fromtimestamp(job.next_fire_at - 1, UTC)
        await session.commit()

    launched = await service.dispatch_due_jobs(now=job.next_fire_at)

    assert launched == ["run-1"]
    launcher.assert_awaited_once()

    async with session_factory() as session:
        refreshed_fire = (await session.execute(select(CronJobFireRow).where(CronJobFireRow.fire_id == fire.fire_id))).scalar_one()

    assert refreshed_fire.claim_owner == "worker-b"
    assert refreshed_fire.status == "dispatched"


class _ConcurrentListCoordinator:
    def __init__(self, target: int) -> None:
        self._target = target
        self._count = 0
        self._lock = anyio.Lock()
        self._event = anyio.Event()

    async def wait(self) -> None:
        async with self._lock:
            self._count += 1
            if self._count >= self._target:
                self._event.set()
        await self._event.wait()


class _CoordinatedRepo:
    def __init__(self, inner: CronSchedulerRepository, coordinator: _ConcurrentListCoordinator) -> None:
        self._inner = inner
        self._coordinator = coordinator

    async def list_due_jobs(self, *, now: float | datetime, limit: int = 100):
        await self._coordinator.wait()
        return await self._inner.list_due_jobs(now=now, limit=limit)

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


@pytest.mark.anyio
async def test_dispatch_due_jobs_concurrent_workers_launch_same_fire_once(session_factory):
    repo = CronSchedulerRepository(session_factory)
    job = await repo.create_job(
        CronJobCreate(
            thread_id="thread-1",
            assistant_id="lead_agent",
            cron="*/5 * * * *",
            timezone="Asia/Shanghai",
        ),
        now=1_746_500_000,
    )
    launcher = AsyncMock(return_value=SimpleNamespace(run_id="run-1"))
    coordinator = _ConcurrentListCoordinator(target=2)
    service_a = CronSchedulerService(_CoordinatedRepo(repo, coordinator), run_launcher=launcher, instance_id="worker-a")
    service_b = CronSchedulerService(_CoordinatedRepo(repo, coordinator), run_launcher=launcher, instance_id="worker-b")

    results: dict[str, list[str]] = {}

    async def run_dispatch(name: str, service: CronSchedulerService) -> None:
        results[name] = await service.dispatch_due_jobs(now=job.next_fire_at)

    async with anyio.create_task_group() as tg:
        tg.start_soon(run_dispatch, "a", service_a)
        tg.start_soon(run_dispatch, "b", service_b)

    assert sorted(results.values()) == [[], ["run-1"]]
    launcher.assert_awaited_once()

    async with session_factory() as session:
        fire = (
            await session.execute(
                select(CronJobFireRow).where(
                    CronJobFireRow.job_id == job.job_id,
                    CronJobFireRow.scheduled_fire_at == datetime.fromtimestamp(job.next_fire_at, UTC),
                )
            )
        ).scalar_one()

    assert fire.status == "dispatched"
    assert fire.run_id == "run-1"
    assert fire.claim_owner in {"worker-a", "worker-b"}


@pytest.mark.anyio
async def test_stale_claim_token_cannot_mark_recovered_fire_dispatched(session_factory):
    repo = CronSchedulerRepository(session_factory)
    job = await repo.create_job(
        CronJobCreate(
            thread_id="thread-1",
            assistant_id="lead_agent",
            cron="*/5 * * * *",
            timezone="Asia/Shanghai",
        ),
        now=1_746_500_000,
    )

    stale_fire = await repo.claim_fire(job.job_id, scheduled_fire_at=job.next_fire_at, instance_id="worker-a", lease_seconds=30)
    assert stale_fire is not None

    async with session_factory() as session:
        fire_row = (await session.execute(select(CronJobFireRow).where(CronJobFireRow.fire_id == stale_fire.fire_id))).scalar_one()
        fire_row.lease_until = datetime.fromtimestamp(job.next_fire_at - 1, UTC)
        await session.commit()

    recovered_fire = await repo.recover_expired_fire(
        job.job_id,
        scheduled_fire_at=job.next_fire_at,
        instance_id="worker-b",
        lease_seconds=30,
        now=job.next_fire_at,
    )

    assert recovered_fire is not None
    assert recovered_fire.claim_token != stale_fire.claim_token

    stale_completed = await repo.mark_fire_dispatched(
        job.job_id,
        stale_fire.fire_id,
        claim_token=stale_fire.claim_token,
        run_id="run-stale",
        fired_at=job.next_fire_at,
    )
    recovered_completed = await repo.mark_fire_dispatched(
        job.job_id,
        recovered_fire.fire_id,
        claim_token=recovered_fire.claim_token,
        run_id="run-fresh",
        fired_at=job.next_fire_at,
    )

    assert stale_completed is False
    assert recovered_completed is True

    async with session_factory() as session:
        refreshed_fire = (await session.execute(select(CronJobFireRow).where(CronJobFireRow.fire_id == stale_fire.fire_id))).scalar_one()
        refreshed_job = (await session.execute(select(CronJobRow).where(CronJobRow.job_id == job.job_id))).scalar_one()

    assert refreshed_fire.run_id == "run-fresh"
    assert refreshed_fire.claim_owner == "worker-b"
    assert refreshed_job.last_run_id == "run-fresh"


@pytest.mark.anyio
async def test_dispatch_due_jobs_does_not_report_launch_when_mark_fire_dispatched_is_fenced(session_factory):
    repo = CronSchedulerRepository(session_factory)
    job = await repo.create_job(
        CronJobCreate(
            thread_id="thread-1",
            assistant_id="lead_agent",
            cron="*/5 * * * *",
            timezone="Asia/Shanghai",
        ),
        now=1_746_500_000,
    )

    async def launcher(job_record, fire_record):
        async with session_factory() as session:
            fire_row = (await session.execute(select(CronJobFireRow).where(CronJobFireRow.fire_id == fire_record.fire_id))).scalar_one()
            fire_row.lease_until = datetime.fromtimestamp(job_record.next_fire_at - 1, UTC)
            await session.commit()

        recovered_fire = await repo.recover_expired_fire(
            job_record.job_id,
            scheduled_fire_at=job_record.next_fire_at,
            instance_id="worker-b",
            lease_seconds=30,
            now=job_record.next_fire_at,
        )
        assert recovered_fire is not None

        recovered_completed = await repo.mark_fire_dispatched(
            job_record.job_id,
            recovered_fire.fire_id,
            claim_token=recovered_fire.claim_token,
            run_id="run-rival",
            fired_at=job_record.next_fire_at,
        )
        assert recovered_completed is True

        return SimpleNamespace(run_id="run-stale")

    service = CronSchedulerService(repo, run_launcher=launcher, instance_id="worker-a", lease_seconds=30)

    launched = await service.dispatch_due_jobs(now=job.next_fire_at)

    assert launched == []

    async with session_factory() as session:
        fire = (
            await session.execute(
                select(CronJobFireRow).where(
                    CronJobFireRow.job_id == job.job_id,
                    CronJobFireRow.scheduled_fire_at == datetime.fromtimestamp(job.next_fire_at, UTC),
                )
            )
        ).scalar_one()
        refreshed_job = (await session.execute(select(CronJobRow).where(CronJobRow.job_id == job.job_id))).scalar_one()

    assert fire.run_id == "run-rival"
    assert fire.claim_owner == "worker-b"
    assert refreshed_job.last_run_id == "run-rival"
