"""Built-in tools for managing cron schedules from agent conversations."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from langchain.tools import tool

from deerflow.persistence.engine import get_session_factory
from deerflow.persistence.scheduler.sql import CronSchedulerRepository
from deerflow.runtime.scheduler import CronJobChannelDelivery, CronJobCreate
from deerflow.runtime.user_context import get_current_user, get_effective_user_id
from deerflow.tools.types import Runtime


def _system_timezone() -> str:
    tzinfo = datetime.now().astimezone().tzinfo
    key = getattr(tzinfo, "key", None)
    if isinstance(key, str) and key:
        return key

    env_tz = os.environ.get("TZ")
    if env_tz:
        return env_tz

    return "UTC"


def _get_scheduler_repo() -> CronSchedulerRepository:
    session_factory = get_session_factory()
    if session_factory is None:
        raise RuntimeError("Cron scheduler is not available because SQL persistence is not configured.")
    return CronSchedulerRepository(session_factory)


def _resolve_thread_id(runtime: Runtime, thread_id: str | None) -> str:
    if thread_id:
        return thread_id

    context = runtime.context or {}
    runtime_thread_id = context.get("thread_id")
    if isinstance(runtime_thread_id, str) and runtime_thread_id:
        return runtime_thread_id

    configured_thread_id = runtime.config.get("configurable", {}).get("thread_id")
    if isinstance(configured_thread_id, str) and configured_thread_id:
        return configured_thread_id

    raise ValueError("thread_id is required when runtime context does not include one.")


def _resolve_creator_user_id(runtime: Runtime) -> str:
    context = runtime.context or {}
    runtime_user_id = context.get("user_id")
    if isinstance(runtime_user_id, str) and runtime_user_id:
        return runtime_user_id

    current_user = get_current_user()
    if current_user is not None:
        return str(current_user.id)

    return get_effective_user_id()


def _format_delivery_summary(delivery: CronJobChannelDelivery | None) -> str:
    if delivery is None:
        return "default thread reply"

    summary = f"channel:{delivery.channel_name} chat={delivery.chat_id}"
    if delivery.thread_ts:
        summary += f" thread={delivery.thread_ts}"
    if delivery.options:
        summary += f" options={','.join(sorted(delivery.options))}"
    return summary


@tool("create_schedule", parse_docstring=True)
async def create_schedule_tool(
    runtime: Runtime,
    cron: str,
    thread_id: str | None = None,
    assistant_id: str | None = None,
    input: dict[str, Any] | None = None,
    delivery: CronJobChannelDelivery | None = None,
) -> str:
    """Create a new cron schedule for the current thread.

    Args:
        cron: Cron expression to use for the schedule.
        thread_id: Optional thread id. Defaults to the current runtime thread.
        assistant_id: Optional assistant identifier to bind to the schedule.
        input: Optional input payload that will be passed to the scheduled run.
        delivery: Optional delivery target for scheduled replies.
    """

    resolved_thread_id = _resolve_thread_id(runtime, thread_id)
    repo = _get_scheduler_repo()
    record = await repo.create_job(
        CronJobCreate(
            thread_id=resolved_thread_id,
            assistant_id=assistant_id,
            creator_user_id=_resolve_creator_user_id(runtime),
            cron=cron,
            timezone=_system_timezone(),
            input=input,
            delivery=delivery,
        )
    )
    return f"Schedule {record.job_id} created for thread {record.thread_id}. Next fire at {record.next_fire_at} ({record.timezone})."


@tool("list_schedules", parse_docstring=True)
async def list_schedules_tool(
    runtime: Runtime,
    thread_id: str | None = None,
    enabled: bool | None = None,
    limit: int = 20,
) -> str:
    """List schedules for a thread.

    Args:
        thread_id: Optional thread id. Defaults to the current runtime thread.
        enabled: Optional enabled filter.
        limit: Maximum number of schedules to return.
    """

    resolved_thread_id = _resolve_thread_id(runtime, thread_id)
    repo = _get_scheduler_repo()
    jobs = await repo.list_jobs(thread_id=resolved_thread_id, enabled=enabled, limit=limit)
    if not jobs:
        return f"No schedules found for thread {resolved_thread_id}."

    lines = [f"Schedules for thread {resolved_thread_id}:"]
    for job in jobs:
        status = "enabled" if job.enabled else "paused"
        lines.append(f"- {job.job_id}: {job.cron} [{status}] next={job.next_fire_at} delivery={_format_delivery_summary(job.delivery)}")
    return "\n".join(lines)


@tool("pause_schedule", parse_docstring=True)
async def pause_schedule_tool(
    runtime: Runtime,
    job_id: str,
    thread_id: str | None = None,
) -> str:
    """Pause an existing schedule by job id.

    Args:
        job_id: Schedule job id.
        thread_id: Optional thread id. Defaults to the current runtime thread.
    """

    resolved_thread_id = _resolve_thread_id(runtime, thread_id)
    repo = _get_scheduler_repo()
    record = await repo.pause_job(job_id, thread_id=resolved_thread_id)
    if record is None:
        return f"Schedule {job_id} not found."
    return f"Schedule {record.job_id} paused."


@tool("resume_schedule", parse_docstring=True)
async def resume_schedule_tool(
    runtime: Runtime,
    job_id: str,
    thread_id: str | None = None,
) -> str:
    """Resume a paused schedule by job id.

    Args:
        job_id: Schedule job id.
        thread_id: Optional thread id. Defaults to the current runtime thread.
    """

    resolved_thread_id = _resolve_thread_id(runtime, thread_id)
    repo = _get_scheduler_repo()
    record = await repo.resume_job(job_id, thread_id=resolved_thread_id)
    if record is None:
        return f"Schedule {job_id} not found."
    return f"Schedule {record.job_id} resumed. Next fire at {record.next_fire_at}."


@tool("delete_schedule", parse_docstring=True)
async def delete_schedule_tool(
    runtime: Runtime,
    job_id: str,
    thread_id: str | None = None,
) -> str:
    """Delete an existing schedule by job id.

    Args:
        job_id: Schedule job id.
        thread_id: Optional thread id. Defaults to the current runtime thread.
    """

    resolved_thread_id = _resolve_thread_id(runtime, thread_id)
    repo = _get_scheduler_repo()
    record = await repo.delete_job(job_id, thread_id=resolved_thread_id)
    if record is None:
        return f"Schedule {job_id} not found."
    return f"Schedule {record.job_id} deleted."
