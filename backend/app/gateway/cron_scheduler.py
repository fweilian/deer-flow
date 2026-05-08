"""Gateway cron scheduler lifecycle helpers."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request

from app.gateway.deps import get_run_context
from app.gateway.services import start_cron_run_with_deps
from deerflow.persistence.engine import get_session_factory
from deerflow.persistence.scheduler.sql import CronSchedulerRepository
from deerflow.runtime.scheduler import CronSchedulerService

logger = logging.getLogger(__name__)


def get_cron_scheduler_repo(request: Request) -> CronSchedulerRepository:
    repo = getattr(request.app.state, "cron_scheduler_repo", None)
    if repo is None:
        raise HTTPException(status_code=503, detail="Cron scheduler not available")
    return repo


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
            thread_id=job.thread_id,
            bridge=app.state.stream_bridge,
            run_mgr=app.state.run_manager,
            run_ctx=get_run_context(SimpleNamespace(app=app)),
        ),
        instance_id=f"gateway-{uuid4().hex}",
    )
    app.state.cron_scheduler_service = service
    app.state.cron_scheduler_task = asyncio.create_task(_scheduler_loop(service))


async def stop_gateway_cron_scheduler(app: FastAPI) -> None:
    task = getattr(app.state, "cron_scheduler_task", None)
    if task is None:
        return

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        app.state.cron_scheduler_task = None
        app.state.cron_scheduler_service = None
