"""Gateway router for cron job management."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from app.gateway.authz import AuthContext, get_auth_context, require_permission
from app.gateway.cron_scheduler import get_cron_scheduler_repo
from app.gateway.deps import get_thread_store
from app.gateway.services import start_cron_run
from deerflow.persistence.scheduler.sql import CronSchedulerRepository
from deerflow.runtime.scheduler import CronJobCreate, CronJobFireRecord, CronJobRecord

router = APIRouter(prefix="/api/cron/jobs", tags=["cron"])


class CronTriggerResponse(BaseModel):
    job_id: str
    run_id: str


async def _require_thread_access(request: Request, thread_id: str, *, require_existing: bool = True) -> None:
    auth = get_auth_context(request)
    if auth is None or not auth.is_authenticated:
        raise HTTPException(status_code=401, detail="Authentication required")

    assert isinstance(auth, AuthContext)
    allowed = await get_thread_store(request).check_access(
        thread_id,
        str(auth.require_user().id),
        require_existing=require_existing,
    )
    if not allowed:
        raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")


@router.post("", response_model=CronJobRecord, status_code=status.HTTP_201_CREATED)
@require_permission("threads", "write")
async def create_cron_job(
    payload: CronJobCreate,
    request: Request,
    repo: CronSchedulerRepository = Depends(get_cron_scheduler_repo),
) -> CronJobRecord:
    await _require_thread_access(request, payload.thread_id, require_existing=True)
    return await repo.create_job(payload)


@router.post("/{job_id}/trigger", response_model=CronTriggerResponse)
@require_permission("runs", "create")
async def trigger_cron_job(
    job_id: str,
    request: Request,
    repo: CronSchedulerRepository = Depends(get_cron_scheduler_repo),
) -> CronTriggerResponse:
    job = await repo.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Cron job {job_id} not found")

    await _require_thread_access(request, job.thread_id, require_existing=True)

    fire = CronJobFireRecord(
        fire_id=f"manual-{uuid4().hex}",
        job_id=job.job_id,
        scheduled_fire_at=datetime.now(UTC).timestamp(),
        status="manual",
    )
    run = await start_cron_run(job, fire, request)
    return CronTriggerResponse(job_id=job.job_id, run_id=run.run_id)
