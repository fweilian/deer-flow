"""Tests for CronSchedulerRepository."""

import pytest

from deerflow.persistence.scheduler.sql import CronSchedulerRepository
from deerflow.runtime.scheduler.schemas import CronJobCreate


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
