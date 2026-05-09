"""Cron scheduler orchestration."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from deerflow.runtime.scheduler.schemas import CronJobFireRecord, CronJobRecord

if TYPE_CHECKING:
    from deerflow.persistence.scheduler.sql import CronSchedulerRepository


logger = logging.getLogger(__name__)


class RunLaunchResult(Protocol):
    run_id: str


RunLauncher = Callable[[CronJobRecord, CronJobFireRecord], Awaitable[RunLaunchResult]]


class CronSchedulerService:
    def __init__(
        self,
        repo: CronSchedulerRepository,
        run_launcher: RunLauncher,
        *,
        instance_id: str,
        poll_interval: float = 10.0,
        lease_seconds: int = 30,
    ) -> None:
        self._repo = repo
        self._run_launcher = run_launcher
        self._instance_id = instance_id
        self._poll_interval = poll_interval
        self._lease_seconds = lease_seconds

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def poll_interval(self) -> float:
        return self._poll_interval

    @property
    def lease_seconds(self) -> int:
        return self._lease_seconds

    async def dispatch_due_jobs(self, *, now: float | None = None, limit: int = 100) -> list[str]:
        launched: list[str] = []
        due_now = now if now is not None else datetime.now(UTC).timestamp()
        for job in await self._repo.list_due_jobs(now=due_now, limit=limit):
            if job.next_fire_at is None:
                continue

            fire = await self._repo.claim_fire(
                job.job_id,
                scheduled_fire_at=job.next_fire_at,
                instance_id=self._instance_id,
                lease_seconds=self._lease_seconds,
            )
            if fire is None:
                fire = await self._repo.recover_expired_fire(
                    job.job_id,
                    scheduled_fire_at=job.next_fire_at,
                    instance_id=self._instance_id,
                    lease_seconds=self._lease_seconds,
                    now=now,
                )
            if fire is None:
                continue

            try:
                run = await self._run_launcher(job, fire)
            except Exception:
                logger.exception(
                    "Cron run launch failed: job_id=%s thread_id=%s fire_id=%s scheduled_fire_at=%s strategy=%s",
                    job.job_id,
                    job.thread_id,
                    fire.fire_id,
                    fire.scheduled_fire_at,
                    job.multitask_strategy,
                )
                raise
            dispatched = await self._repo.mark_fire_dispatched(
                job.job_id,
                fire.fire_id,
                claim_token=fire.claim_token,
                run_id=run.run_id,
                fired_at=job.next_fire_at,
            )
            if dispatched:
                launched.append(run.run_id)

        return launched
