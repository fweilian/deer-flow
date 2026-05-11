"""SQLAlchemy-backed cron scheduler repository."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.scheduler.model import CronJobFireRow, CronJobRow
from deerflow.runtime.scheduler.schemas import CronJobCreate, CronJobFireRecord, CronJobRecord, compute_next_fire_at

CronMultitaskStrategy = Literal["reject", "interrupt", "rollback", "enqueue"]
_CRON_INTERNAL_METADATA_KEY = "_cron"


def _extract_execution_thread_id(metadata: dict | None, fallback_thread_id: str) -> str:
    internal = metadata.get(_CRON_INTERNAL_METADATA_KEY) if isinstance(metadata, dict) else None
    if isinstance(internal, dict):
        execution_thread_id = internal.get("execution_thread_id")
        if isinstance(execution_thread_id, str) and execution_thread_id.strip():
            return execution_thread_id.strip()
    return fallback_thread_id


def _strip_internal_metadata(metadata: dict | None) -> dict:
    if not isinstance(metadata, dict):
        return {}
    clean = dict(metadata)
    clean.pop(_CRON_INTERNAL_METADATA_KEY, None)
    return clean


def _inject_execution_thread_id(metadata: dict | None, execution_thread_id: str) -> dict:
    enriched = dict(metadata or {})
    internal = dict(enriched.get(_CRON_INTERNAL_METADATA_KEY) or {})
    internal["execution_thread_id"] = execution_thread_id
    enriched[_CRON_INTERNAL_METADATA_KEY] = internal
    return enriched


def _coerce_datetime(value: float | datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    return datetime.fromtimestamp(value, UTC)


def _datetime_to_timestamp(value: datetime | None) -> float | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    return value.timestamp()


class CronSchedulerRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    @staticmethod
    def _row_to_record(row: CronJobRow) -> CronJobRecord:
        return CronJobRecord(
            job_id=row.job_id,
            thread_id=row.thread_id,
            execution_thread_id=_extract_execution_thread_id(row.metadata_json, row.thread_id),
            assistant_id=row.assistant_id,
            creator_user_id=row.creator_user_id,
            cron=row.cron_expr,
            timezone=row.timezone,
            enabled=row.enabled,
            input=row.input_json,
            metadata=_strip_internal_metadata(row.metadata_json),
            config=row.config_json,
            context=row.context_json,
            delivery=row.delivery_json,
            multitask_strategy=cast(CronMultitaskStrategy, row.multitask_strategy),
            next_fire_at=_datetime_to_timestamp(row.next_fire_at),
            last_fire_at=_datetime_to_timestamp(row.last_fire_at),
            last_run_id=row.last_run_id,
            created_at=_datetime_to_timestamp(row.created_at) or 0.0,
            updated_at=_datetime_to_timestamp(row.updated_at) or 0.0,
        )

    @staticmethod
    def _fire_row_to_record(row: CronJobFireRow) -> CronJobFireRecord:
        return CronJobFireRecord(
            fire_id=row.fire_id,
            job_id=row.job_id,
            scheduled_fire_at=_datetime_to_timestamp(row.scheduled_fire_at) or 0.0,
            status=row.status,
            claim_owner=row.claim_owner,
            claim_token=row.claim_token,
            lease_until=_datetime_to_timestamp(row.lease_until),
            run_id=row.run_id,
            error=row.error,
            delivery_status=cast(Literal["pending", "sent", "failed"] | None, row.delivery_status),
            delivery_error=row.delivery_error,
            delivery_attempted_at=_datetime_to_timestamp(row.delivery_attempted_at),
        )

    async def create_job(
        self,
        payload: CronJobCreate,
        *,
        now: float | datetime | None = None,
    ) -> CronJobRecord:
        created_at = _coerce_datetime(now)
        execution_thread_id = (payload.execution_thread_id or "").strip() or str(uuid4())
        row = CronJobRow(
            job_id=uuid4().hex,
            thread_id=payload.thread_id,
            assistant_id=payload.assistant_id,
            creator_user_id=payload.creator_user_id,
            cron_expr=payload.cron,
            timezone=payload.timezone,
            enabled=payload.enabled,
            input_json=payload.input,
            metadata_json=_inject_execution_thread_id(payload.metadata, execution_thread_id),
            config_json=payload.config,
            context_json=payload.context,
            delivery_json=payload.delivery.model_dump() if payload.delivery is not None else None,
            multitask_strategy=payload.multitask_strategy,
            next_fire_at=_coerce_datetime(compute_next_fire_at(payload.cron, payload.timezone, now=created_at)),
            created_at=created_at,
            updated_at=created_at,
        )
        async with self._sf() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return self._row_to_record(row)

    async def list_due_jobs(
        self,
        *,
        now: float | datetime,
        limit: int = 100,
    ) -> list[CronJobRecord]:
        due_at = _coerce_datetime(now)
        stmt = (
            select(CronJobRow)
            .where(
                CronJobRow.enabled.is_(True),
                CronJobRow.next_fire_at.is_not(None),
                CronJobRow.next_fire_at <= due_at,
            )
            .order_by(CronJobRow.next_fire_at.asc(), CronJobRow.created_at.asc())
            .limit(limit)
        )
        async with self._sf() as session:
            result = await session.execute(stmt)
            return [self._row_to_record(row) for row in result.scalars()]

    async def get_job(self, job_id: str) -> CronJobRecord | None:
        async with self._sf() as session:
            row = (await session.execute(select(CronJobRow).where(CronJobRow.job_id == job_id))).scalar_one_or_none()
            if row is None:
                return None
            return self._row_to_record(row)

    async def list_jobs(
        self,
        *,
        thread_id: str,
        enabled: bool | None = None,
        limit: int = 100,
    ) -> list[CronJobRecord]:
        stmt = select(CronJobRow).where(CronJobRow.thread_id == thread_id).order_by(CronJobRow.created_at.asc()).limit(limit)
        if enabled is not None:
            stmt = stmt.where(CronJobRow.enabled.is_(enabled))
        async with self._sf() as session:
            result = await session.execute(stmt)
            return [self._row_to_record(row) for row in result.scalars()]

    async def pause_job(self, job_id: str, *, thread_id: str) -> CronJobRecord | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(CronJobRow).where(
                        CronJobRow.job_id == job_id,
                        CronJobRow.thread_id == thread_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            row.enabled = False
            row.next_fire_at = None
            row.updated_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(row)
            return self._row_to_record(row)

    async def resume_job(
        self,
        job_id: str,
        *,
        thread_id: str,
        now: float | datetime | None = None,
    ) -> CronJobRecord | None:
        resumed_at = _coerce_datetime(now)
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(CronJobRow).where(
                        CronJobRow.job_id == job_id,
                        CronJobRow.thread_id == thread_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            if row.enabled and row.next_fire_at is not None:
                return self._row_to_record(row)
            row.enabled = True
            row.next_fire_at = _coerce_datetime(compute_next_fire_at(row.cron_expr, row.timezone, now=resumed_at))
            row.updated_at = resumed_at
            await session.commit()
            await session.refresh(row)
            return self._row_to_record(row)

    async def delete_job(self, job_id: str, *, thread_id: str) -> CronJobRecord | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(CronJobRow).where(
                        CronJobRow.job_id == job_id,
                        CronJobRow.thread_id == thread_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            record = self._row_to_record(row)
            await session.execute(
                delete(CronJobRow).where(
                    CronJobRow.job_id == job_id,
                    CronJobRow.thread_id == thread_id,
                )
            )
            await session.commit()
            return record

    async def update_job(
        self,
        job_id: str,
        *,
        thread_id: str,
        cron: str | None = None,
        assistant_id: str | None = None,
        input: dict | None = None,
        delivery=None,
        now: float | datetime | None = None,
    ) -> CronJobRecord | None:
        updated_at = _coerce_datetime(now)
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(CronJobRow).where(
                        CronJobRow.job_id == job_id,
                        CronJobRow.thread_id == thread_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None

            if cron is not None:
                row.cron_expr = cron
                if row.enabled:
                    row.next_fire_at = _coerce_datetime(compute_next_fire_at(row.cron_expr, row.timezone, now=updated_at))
            if assistant_id is not None:
                row.assistant_id = assistant_id
            if input is not None:
                row.input_json = input
            if delivery is not None:
                row.delivery_json = delivery.model_dump()

            row.updated_at = updated_at
            await session.commit()
            await session.refresh(row)
            return self._row_to_record(row)

    async def claim_fire(
        self,
        job_id: str,
        *,
        scheduled_fire_at: float | datetime,
        instance_id: str,
        lease_seconds: int,
    ) -> CronJobFireRecord | None:
        fire_time = _coerce_datetime(scheduled_fire_at)
        claimed_at = datetime.now(UTC)
        row = CronJobFireRow(
            fire_id=uuid4().hex,
            job_id=job_id,
            scheduled_fire_at=fire_time,
            status="claimed",
            claim_owner=instance_id,
            claim_token=uuid4().hex,
            lease_until=claimed_at + timedelta(seconds=lease_seconds),
        )
        async with self._sf() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return None
            await session.refresh(row)
            return self._fire_row_to_record(row)

    async def recover_expired_fire(
        self,
        job_id: str,
        *,
        scheduled_fire_at: float | datetime,
        instance_id: str,
        lease_seconds: int,
        now: float | datetime | None = None,
    ) -> CronJobFireRecord | None:
        fire_time = _coerce_datetime(scheduled_fire_at)
        recovery_at = _coerce_datetime(now)
        lease_until = recovery_at + timedelta(seconds=lease_seconds)
        claim_token = uuid4().hex
        stmt = (
            update(CronJobFireRow)
            .where(
                CronJobFireRow.job_id == job_id,
                CronJobFireRow.scheduled_fire_at == fire_time,
                CronJobFireRow.status == "claimed",
                CronJobFireRow.lease_until.is_not(None),
                CronJobFireRow.lease_until <= recovery_at,
            )
            .values(
                claim_owner=instance_id,
                claim_token=claim_token,
                lease_until=lease_until,
            )
        )
        async with self._sf() as session:
            result = await session.execute(stmt)
            if result.rowcount == 0:
                await session.rollback()
                return None
            fire = (
                await session.execute(
                    select(CronJobFireRow).where(
                        CronJobFireRow.job_id == job_id,
                        CronJobFireRow.scheduled_fire_at == fire_time,
                    )
                )
            ).scalar_one()
            await session.commit()
            return self._fire_row_to_record(fire)

    async def mark_fire_dispatched(
        self,
        job_id: str,
        fire_id: str,
        *,
        claim_token: str | None,
        run_id: str,
        fired_at: float | datetime,
    ) -> bool:
        fired_time = _coerce_datetime(fired_at)
        async with self._sf() as session:
            fire = (
                await session.execute(
                    select(CronJobFireRow).where(
                        CronJobFireRow.job_id == job_id,
                        CronJobFireRow.fire_id == fire_id,
                    )
                )
            ).scalar_one()
            if fire.status != "claimed" or fire.claim_token != claim_token:
                await session.rollback()
                return False

            job = (await session.execute(select(CronJobRow).where(CronJobRow.job_id == job_id))).scalar_one()

            fire.status = "dispatched"
            fire.run_id = run_id
            fire.lease_until = None
            job.last_fire_at = fired_time
            job.last_run_id = run_id
            job.next_fire_at = _coerce_datetime(compute_next_fire_at(job.cron_expr, job.timezone, now=fired_time))
            job.updated_at = datetime.now(UTC)

            await session.commit()
            return True

    async def mark_fire_skipped(
        self,
        job_id: str,
        fire_id: str,
        *,
        claim_token: str | None,
        skipped_at: float | datetime,
        error: str,
    ) -> bool:
        skipped_time = _coerce_datetime(skipped_at)
        async with self._sf() as session:
            fire = (
                await session.execute(
                    select(CronJobFireRow).where(
                        CronJobFireRow.job_id == job_id,
                        CronJobFireRow.fire_id == fire_id,
                    )
                )
            ).scalar_one()
            if fire.status != "claimed" or fire.claim_token != claim_token:
                await session.rollback()
                return False

            job = (await session.execute(select(CronJobRow).where(CronJobRow.job_id == job_id))).scalar_one()

            fire.status = "skipped"
            fire.error = error
            fire.lease_until = None
            job.next_fire_at = _coerce_datetime(compute_next_fire_at(job.cron_expr, job.timezone, now=skipped_time))
            job.updated_at = datetime.now(UTC)

            await session.commit()
            return True

    async def mark_fire_delivery(
        self,
        job_id: str,
        fire_id: str,
        *,
        delivery_status: Literal["pending", "sent", "failed"],
        delivery_error: str | None = None,
        attempted_at: float | datetime | None = None,
    ) -> bool:
        delivery_time = _coerce_datetime(attempted_at)
        async with self._sf() as session:
            fire = (
                await session.execute(
                    select(CronJobFireRow).where(
                        CronJobFireRow.job_id == job_id,
                        CronJobFireRow.fire_id == fire_id,
                    )
                )
            ).scalar_one_or_none()
            if fire is None:
                await session.rollback()
                return False

            fire.delivery_status = delivery_status
            fire.delivery_error = delivery_error
            fire.delivery_attempted_at = delivery_time
            await session.commit()
            return True
