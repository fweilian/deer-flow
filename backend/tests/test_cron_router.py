from unittest.mock import AsyncMock, MagicMock

from _router_auth_helpers import make_authed_test_app
from fastapi.testclient import TestClient

from deerflow.runtime.scheduler.schemas import CronJobRecord


def _make_app(*, scheduler_repo=None):
    from app.gateway.routers import cron

    app = make_authed_test_app()
    app.include_router(cron.router)
    if scheduler_repo is not None:
        app.state.cron_scheduler_repo = scheduler_repo
    return app


def test_create_cron_job_route():
    repo = MagicMock()
    repo.create_job = AsyncMock(
        return_value=CronJobRecord(
            job_id="job-1",
            thread_id="thread-1",
            assistant_id="lead_agent",
            cron="*/5 * * * *",
            timezone="Asia/Shanghai",
            enabled=True,
            input={"messages": [{"role": "user", "content": "hi"}]},
            metadata={"source": "test"},
            config={"tags": ["nightly"]},
            context={"agent_name": "lead-agent"},
            multitask_strategy="enqueue",
            next_fire_at=1_746_500_300.0,
            last_fire_at=None,
            last_run_id=None,
            created_at=1_746_500_000.0,
            updated_at=1_746_500_000.0,
        )
    )
    app = _make_app(scheduler_repo=repo)

    with TestClient(app) as client:
        response = client.post(
            "/api/cron/jobs",
            json={
                "thread_id": "thread-1",
                "assistant_id": "lead_agent",
                "cron": "*/5 * * * *",
                "timezone": "Asia/Shanghai",
                "input": {"messages": [{"role": "user", "content": "hi"}]},
                "metadata": {"source": "test"},
                "config": {"tags": ["nightly"]},
                "context": {"agent_name": "lead-agent"},
                "multitask_strategy": "enqueue",
            },
        )

    assert response.status_code == 201, response.text
    assert response.json() == {
        "job_id": "job-1",
        "thread_id": "thread-1",
        "assistant_id": "lead_agent",
        "cron": "*/5 * * * *",
        "timezone": "Asia/Shanghai",
        "enabled": True,
        "input": {"messages": [{"role": "user", "content": "hi"}]},
        "metadata": {"source": "test"},
        "config": {"tags": ["nightly"]},
        "context": {"agent_name": "lead-agent"},
        "multitask_strategy": "enqueue",
        "next_fire_at": 1_746_500_300.0,
        "last_fire_at": None,
        "last_run_id": None,
        "created_at": 1_746_500_000.0,
        "updated_at": 1_746_500_000.0,
    }
    repo.create_job.assert_awaited_once()


def test_trigger_cron_job_route(monkeypatch):
    repo = MagicMock()
    repo.get_job = AsyncMock(
        return_value=CronJobRecord(
            job_id="job-1",
            thread_id="thread-1",
            assistant_id="lead_agent",
            cron="*/5 * * * *",
            timezone="Asia/Shanghai",
            enabled=True,
            input=None,
            metadata={},
            config=None,
            context=None,
            multitask_strategy="enqueue",
            next_fire_at=1_746_500_300.0,
            last_fire_at=None,
            last_run_id=None,
            created_at=1_746_500_000.0,
            updated_at=1_746_500_000.0,
        )
    )
    app = _make_app(scheduler_repo=repo)

    launcher = AsyncMock(return_value=MagicMock(run_id="run-1"))
    monkeypatch.setattr("app.gateway.routers.cron.start_cron_run", launcher)

    with TestClient(app) as client:
        response = client.post("/api/cron/jobs/job-1/trigger")

    assert response.status_code == 200, response.text
    assert response.json() == {"job_id": "job-1", "run_id": "run-1"}
    repo.get_job.assert_awaited_once_with("job-1")
    launcher.assert_awaited_once()
