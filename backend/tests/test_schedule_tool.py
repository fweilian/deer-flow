from __future__ import annotations

import pytest
from langchain.tools import ToolRuntime

from deerflow.persistence.engine import close_engine, get_session_factory, init_engine
from deerflow.persistence.scheduler.sql import CronSchedulerRepository
from deerflow.runtime.user_context import reset_current_user, set_current_user
from deerflow.tools.builtins.schedule_tool import (
    create_schedule_tool,
    delete_schedule_tool,
    list_schedules_tool,
    pause_schedule_tool,
    resume_schedule_tool,
)


def _make_runtime(*, thread_id: str | None = None) -> ToolRuntime:
    context = {"thread_id": thread_id} if thread_id is not None else {}
    configurable = {"thread_id": thread_id} if thread_id is not None else {}
    return ToolRuntime(
        state={"thread_data": {}, "sandbox": {}},
        context=context,
        config={"configurable": configurable},
        stream_writer=lambda _: None,
        tools=[],
        tool_call_id="call-1",
        store=None,
    )


@pytest.fixture
async def scheduler_repo(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield CronSchedulerRepository(get_session_factory())
    finally:
        await close_engine()


@pytest.mark.anyio
async def test_create_schedule_then_pause_schedule(scheduler_repo, monkeypatch):
    monkeypatch.setattr("deerflow.tools.builtins.schedule_tool._get_scheduler_repo", lambda: scheduler_repo)
    monkeypatch.setattr("deerflow.tools.builtins.schedule_tool._system_timezone", lambda: "Asia/Shanghai")

    created = await create_schedule_tool.ainvoke(
        {
            "runtime": _make_runtime(thread_id="thread-create"),
            "cron": "*/15 * * * *",
            "assistant_id": "lead_agent",
            "delivery": {
                "kind": "channel",
                "channel_name": "webhook",
                "chat_id": "ops-room",
                "thread_ts": "ops-thread",
                "options": {"api_request": {"method": "POST", "path": "/hooks/schedule"}},
            },
        }
    )

    assert "created" in created.lower()

    jobs = await scheduler_repo.list_jobs(thread_id="thread-create")
    assert len(jobs) == 1
    assert jobs[0].enabled is True
    assert jobs[0].timezone == "Asia/Shanghai"
    assert jobs[0].creator_user_id == "test-user-autouse"
    assert jobs[0].delivery is not None
    assert jobs[0].delivery.channel_name == "webhook"
    assert jobs[0].delivery.options["api_request"]["path"] == "/hooks/schedule"

    paused = await pause_schedule_tool.ainvoke({"runtime": _make_runtime(thread_id="thread-create"), "job_id": jobs[0].job_id})

    assert "paused" in paused.lower()

    refreshed = await scheduler_repo.get_job(jobs[0].job_id)
    assert refreshed is not None
    assert refreshed.enabled is False


@pytest.mark.anyio
async def test_create_schedule_uses_runtime_thread_by_default(scheduler_repo, monkeypatch):
    monkeypatch.setattr("deerflow.tools.builtins.schedule_tool._get_scheduler_repo", lambda: scheduler_repo)
    monkeypatch.setattr("deerflow.tools.builtins.schedule_tool._system_timezone", lambda: "UTC")

    created = await create_schedule_tool.ainvoke(
        {
            "runtime": _make_runtime(thread_id="thread-from-runtime"),
            "cron": "0 * * * *",
        }
    )

    assert "thread-from-runtime" in created

    listed = await list_schedules_tool.ainvoke({"runtime": _make_runtime(thread_id="thread-from-runtime")})
    assert "thread-from-runtime" in listed
    assert "delivery=default thread reply" in listed


@pytest.mark.anyio
async def test_schedule_mutation_tools_are_scoped_to_runtime_thread(scheduler_repo, monkeypatch):
    monkeypatch.setattr("deerflow.tools.builtins.schedule_tool._get_scheduler_repo", lambda: scheduler_repo)
    monkeypatch.setattr("deerflow.tools.builtins.schedule_tool._system_timezone", lambda: "UTC")

    await create_schedule_tool.ainvoke(
        {
            "runtime": _make_runtime(thread_id="thread-owner"),
            "cron": "0 * * * *",
        }
    )
    jobs = await scheduler_repo.list_jobs(thread_id="thread-owner")
    assert len(jobs) == 1

    paused = await pause_schedule_tool.ainvoke({"runtime": _make_runtime(thread_id="thread-other"), "job_id": jobs[0].job_id})
    assert paused == f"Schedule {jobs[0].job_id} not found."

    resumed = await resume_schedule_tool.ainvoke({"runtime": _make_runtime(thread_id="thread-other"), "job_id": jobs[0].job_id})
    assert resumed == f"Schedule {jobs[0].job_id} not found."

    refreshed = await scheduler_repo.get_job(jobs[0].job_id)
    assert refreshed is not None
    assert refreshed.enabled is True

    deleted = await delete_schedule_tool.ainvoke({"runtime": _make_runtime(thread_id="thread-other"), "job_id": jobs[0].job_id})
    assert deleted == f"Schedule {jobs[0].job_id} not found."


@pytest.mark.anyio
async def test_resume_schedule_keeps_existing_due_fire_when_job_is_already_enabled(scheduler_repo, monkeypatch):
    monkeypatch.setattr("deerflow.tools.builtins.schedule_tool._get_scheduler_repo", lambda: scheduler_repo)
    monkeypatch.setattr("deerflow.tools.builtins.schedule_tool._system_timezone", lambda: "UTC")

    await create_schedule_tool.ainvoke(
        {
            "runtime": _make_runtime(thread_id="thread-resume"),
            "cron": "0 * * * *",
        }
    )
    jobs = await scheduler_repo.list_jobs(thread_id="thread-resume")
    assert len(jobs) == 1
    original_next_fire_at = jobs[0].next_fire_at

    resumed = await resume_schedule_tool.ainvoke({"runtime": _make_runtime(thread_id="thread-resume"), "job_id": jobs[0].job_id})
    assert "resumed" in resumed.lower()

    refreshed = await scheduler_repo.get_job(jobs[0].job_id)
    assert refreshed is not None
    assert refreshed.enabled is True
    assert refreshed.next_fire_at == original_next_fire_at


@pytest.mark.anyio
@pytest.mark.no_auto_user
async def test_create_schedule_defaults_creator_user_id_without_current_user(scheduler_repo, monkeypatch):
    monkeypatch.setattr("deerflow.tools.builtins.schedule_tool._get_scheduler_repo", lambda: scheduler_repo)
    monkeypatch.setattr("deerflow.tools.builtins.schedule_tool._system_timezone", lambda: "UTC")

    created = await create_schedule_tool.ainvoke(
        {
            "runtime": _make_runtime(thread_id="thread-default-user"),
            "cron": "0 * * * *",
        }
    )

    assert "created" in created.lower()
    jobs = await scheduler_repo.list_jobs(thread_id="thread-default-user")
    assert len(jobs) == 1
    assert jobs[0].creator_user_id == "default"


@pytest.mark.anyio
async def test_create_schedule_uses_current_user_context_for_creator_user_id(scheduler_repo, monkeypatch):
    monkeypatch.setattr("deerflow.tools.builtins.schedule_tool._get_scheduler_repo", lambda: scheduler_repo)
    monkeypatch.setattr("deerflow.tools.builtins.schedule_tool._system_timezone", lambda: "UTC")

    token = set_current_user(type("User", (), {"id": "schedule-user-42"})())
    try:
        await create_schedule_tool.ainvoke(
            {
                "runtime": _make_runtime(thread_id="thread-user-stamped"),
                "cron": "0 * * * *",
            }
        )
    finally:
        reset_current_user(token)

    jobs = await scheduler_repo.list_jobs(thread_id="thread-user-stamped")
    assert len(jobs) == 1
    assert jobs[0].creator_user_id == "schedule-user-42"
