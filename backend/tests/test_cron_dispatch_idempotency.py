import pytest

from deerflow.persistence.engine import close_engine, get_session_factory, init_engine
from deerflow.persistence.run import RunRepository


@pytest.fixture
async def session_factory(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


@pytest.mark.anyio
async def test_start_cron_run_reuses_existing_run(session_factory):
    from app.gateway.services import find_existing_cron_run

    run_repo = RunRepository(session_factory)
    await run_repo.put(
        "run-1",
        thread_id="thread-1",
        status="success",
        metadata={"scheduler": {"idempotency_key": "cron:job-1:1746500000"}},
    )

    reused = await find_existing_cron_run(run_repo, "thread-1", "cron:job-1:1746500000")

    assert reused is not None
    assert reused["run_id"] == "run-1"


@pytest.mark.anyio
async def test_start_cron_run_returns_none_when_idempotency_key_missing(session_factory):
    from app.gateway.services import find_existing_cron_run

    run_repo = RunRepository(session_factory)
    await run_repo.put(
        "run-1",
        thread_id="thread-1",
        status="success",
        metadata={"scheduler": {"idempotency_key": "cron:job-1:1746500000"}},
    )

    reused = await find_existing_cron_run(run_repo, "thread-1", "cron:job-1:9999999999")

    assert reused is None
