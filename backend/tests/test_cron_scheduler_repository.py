"""Tests for CronSchedulerRepository."""

import pytest

from deerflow.persistence.scheduler.sql import CronSchedulerRepository
from deerflow.runtime.scheduler.schemas import CronJobCreate, compute_next_fire_at


async def _make_repo(tmp_path):
    from deerflow.persistence.engine import get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    return CronSchedulerRepository(get_session_factory())


async def _cleanup():
    from deerflow.persistence.engine import close_engine

    await close_engine()


class TestCronSchedulerRepository:
    @pytest.mark.anyio
    async def test_create_job_and_list_due_jobs(self, tmp_path):
        repo = await _make_repo(tmp_path)
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
        await _cleanup()

    @pytest.mark.anyio
    async def test_create_job_preserves_optional_payload_fields(self, tmp_path):
        repo = await _make_repo(tmp_path)

        missing = await repo.create_job(
            CronJobCreate(
                thread_id="thread-missing",
                cron="*/5 * * * *",
                timezone="Asia/Shanghai",
            ),
            now=1_746_500_000,
        )
        empty = await repo.create_job(
            CronJobCreate(
                thread_id="thread-empty",
                cron="*/5 * * * *",
                timezone="Asia/Shanghai",
                input={},
                config={},
                context={},
            ),
            now=1_746_500_000,
        )

        assert missing.input is None
        assert missing.config is None
        assert missing.context is None
        assert empty.input == {}
        assert empty.config == {}
        assert empty.context == {}
        await _cleanup()

    @pytest.mark.anyio
    async def test_list_due_jobs_excludes_disabled_and_future_jobs(self, tmp_path):
        repo = await _make_repo(tmp_path)
        now = 1_746_500_000

        due_job = await repo.create_job(
            CronJobCreate(
                thread_id="thread-due",
                cron="*/5 * * * *",
                timezone="Asia/Shanghai",
            ),
            now=now,
        )
        due_fire_at = due_job.next_fire_at
        await repo.create_job(
            CronJobCreate(
                thread_id="thread-disabled",
                cron="*/5 * * * *",
                timezone="Asia/Shanghai",
                enabled=False,
            ),
            now=now,
        )
        future_now = due_fire_at + 60
        await repo.create_job(
            CronJobCreate(
                thread_id="thread-future",
                cron="*/5 * * * *",
                timezone="Asia/Shanghai",
            ),
            now=future_now,
        )

        due = await repo.list_due_jobs(now=due_fire_at)
        assert [job.thread_id for job in due] == ["thread-due"]
        await _cleanup()

    @pytest.mark.anyio
    async def test_list_due_jobs_orders_by_next_fire_at_then_created_at(self, tmp_path):
        repo = await _make_repo(tmp_path)
        now = 1_746_500_000
        expected_first_fire = compute_next_fire_at("*/5 * * * *", "Asia/Shanghai", now=now)

        first_created = await repo.create_job(
            CronJobCreate(
                thread_id="thread-first-created",
                cron="*/5 * * * *",
                timezone="Asia/Shanghai",
            ),
            now=now,
        )
        second_created = await repo.create_job(
            CronJobCreate(
                thread_id="thread-second-created",
                cron="*/5 * * * *",
                timezone="Asia/Shanghai",
            ),
            now=now + 10,
        )
        later_fire = await repo.create_job(
            CronJobCreate(
                thread_id="thread-later-fire",
                cron="*/10 * * * *",
                timezone="Asia/Shanghai",
            ),
            now=now,
        )

        due = await repo.list_due_jobs(now=later_fire.next_fire_at)
        assert expected_first_fire == first_created.next_fire_at == second_created.next_fire_at
        assert [job.thread_id for job in due] == [
            "thread-first-created",
            "thread-second-created",
            "thread-later-fire",
        ]
        await _cleanup()

    @pytest.mark.anyio
    async def test_list_due_jobs_respects_limit(self, tmp_path):
        repo = await _make_repo(tmp_path)
        now = 1_746_500_000

        await repo.create_job(
            CronJobCreate(
                thread_id="thread-1",
                cron="*/5 * * * *",
                timezone="Asia/Shanghai",
            ),
            now=now,
        )
        second = await repo.create_job(
            CronJobCreate(
                thread_id="thread-2",
                cron="*/5 * * * *",
                timezone="Asia/Shanghai",
            ),
            now=now + 10,
        )
        await repo.create_job(
            CronJobCreate(
                thread_id="thread-3",
                cron="*/10 * * * *",
                timezone="Asia/Shanghai",
            ),
            now=now,
        )

        due = await repo.list_due_jobs(now=second.next_fire_at, limit=2)
        assert [job.thread_id for job in due] == ["thread-1", "thread-2"]
        assert len(due) == 2
        await _cleanup()
