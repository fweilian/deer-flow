"""SQLAlchemy-backed cron scheduler repository."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.scheduler.model import CronJobRow
from deerflow.runtime.scheduler.schemas import CronJobCreate, CronJobRecord, compute_next_fire_at


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
            assistant_id=row.assistant_id,
            cron=row.cron_expr,
            timezone=row.timezone,
            enabled=row.enabled,
            input=row.input_json or None,
            metadata=row.metadata_json or {},
            config=row.config_json or None,
            context=row.context_json or None,
            multitask_strategy=row.multitask_strategy,  # type: ignore[arg-type]
            next_fire_at=_datetime_to_timestamp(row.next_fire_at),
            last_fire_at=_datetime_to_timestamp(row.last_fire_at),
            last_run_id=row.last_run_id,
            created_at=_datetime_to_timestamp(row.created_at) or 0.0,
            updated_at=_datetime_to_timestamp(row.updated_at) or 0.0,
        )

    async def create_job(
        self,
        payload: CronJobCreate,
        *,
        now: float | datetime | None = None,
    ) -> CronJobRecord:
        created_at = _coerce_datetime(now)
        row = CronJobRow(
            job_id=uuid4().hex,
            thread_id=payload.thread_id,
            assistant_id=payload.assistant_id,
            cron_expr=payload.cron,
            timezone=payload.timezone,
            enabled=payload.enabled,
            input_json=payload.input or {},
            metadata_json=payload.metadata,
            config_json=payload.config or {},
            context_json=payload.context or {},
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
