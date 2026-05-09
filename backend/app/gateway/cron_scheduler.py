"""Gateway cron scheduler lifecycle helpers."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request

from app.gateway.services import start_cron_run, start_cron_run_with_deps
from deerflow.persistence.engine import get_session_factory
from deerflow.persistence.scheduler.sql import CronSchedulerRepository
from deerflow.runtime import RunContext
from deerflow.runtime.scheduler import CronJobFireRecord, CronJobRecord, CronSchedulerService

logger = logging.getLogger(__name__)


def get_cron_scheduler_repo(request: Request) -> CronSchedulerRepository:
    repo = getattr(request.app.state, "cron_scheduler_repo", None)
    if repo is None:
        raise HTTPException(status_code=503, detail="Cron scheduler not available")
    return repo


def build_gateway_run_context(app: FastAPI) -> RunContext:
    from app.channels.service import get_channel_service

    channel_service = get_channel_service()
    return RunContext(
        checkpointer=app.state.checkpointer,
        store=getattr(app.state, "store", None),
        event_store=app.state.run_event_store,
        run_events_config=getattr(app.state.config, "run_events", None),
        thread_store=app.state.thread_store,
        app_config=app.state.config,
        outbound_publisher=channel_service.bus.publish_outbound if channel_service is not None else None,
        cron_scheduler_repo=getattr(app.state, "cron_scheduler_repo", None),
    )


async def get_gateway_cron_job(
    job_id: str,
    repo: CronSchedulerRepository,
) -> CronJobRecord:
    job = await repo.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Cron job {job_id} not found")
    return job


async def trigger_gateway_cron_job(
    job: CronJobRecord,
    request: Request,
):
    fire = CronJobFireRecord(
        fire_id=f"manual-{uuid4().hex}",
        job_id=job.job_id,
        scheduled_fire_at=datetime.now(UTC).timestamp(),
        status="manual",
    )
    return await start_cron_run(job, fire, request)


async def _scheduler_loop(service: CronSchedulerService) -> None:
    while True:
        try:
            await service.dispatch_due_jobs()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Gateway cron scheduler dispatch failed")

        await asyncio.sleep(service.poll_interval)


async def start_gateway_cron_scheduler(app: FastAPI) -> None:
    if getattr(app.state, "cron_scheduler_task", None) is not None:
        return

    sf = get_session_factory()
    if sf is None:
        logger.info("Cron scheduler disabled: no SQL session factory available")
        return

    repo = CronSchedulerRepository(sf)
    app.state.cron_scheduler_repo = repo

    service = CronSchedulerService(
        repo,
        run_launcher=lambda job, fire: start_cron_run_with_deps(
            job,
            fire,
            thread_id=getattr(job, "execution_thread_id", None) or job.thread_id,
            bridge=app.state.stream_bridge,
            run_mgr=app.state.run_manager,
            run_ctx=build_gateway_run_context(app),
        ),
        instance_id=f"gateway-{uuid4().hex}",
    )
    app.state.cron_scheduler_service = service
    app.state.cron_scheduler_task = asyncio.create_task(_scheduler_loop(service))
    logger.info(
        "Gateway cron scheduler started: instance_id=%s poll_interval=%.1fs lease_seconds=%ss",
        service.instance_id,
        service.poll_interval,
        service.lease_seconds,
    )


async def stop_gateway_cron_scheduler(app: FastAPI) -> None:
    task = getattr(app.state, "cron_scheduler_task", None)
    if task is None:
        app.state.cron_scheduler_task = None
        app.state.cron_scheduler_service = None
        app.state.cron_scheduler_repo = None
        return

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        app.state.cron_scheduler_task = None
        app.state.cron_scheduler_service = None
        app.state.cron_scheduler_repo = None
