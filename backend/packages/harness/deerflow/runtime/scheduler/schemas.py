"""Scheduler request/record schemas and cron timing helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from pydantic import BaseModel, Field, field_validator


def _coerce_now(now: float | datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if isinstance(now, datetime):
        if now.tzinfo is None:
            return now.replace(tzinfo=UTC)
        return now.astimezone(UTC)
    return datetime.fromtimestamp(now, UTC)


def _validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {value}") from exc
    return value


def _validate_cron(value: str) -> str:
    if not croniter.is_valid(value):
        raise ValueError(f"Invalid cron expression: {value}")
    return value


def compute_next_fire_at(cron: str, timezone: str, *, now: float | datetime | None = None) -> float:
    """Return the next scheduled fire time as a UTC unix timestamp."""

    cron_expr = _validate_cron(cron)
    tz_name = _validate_timezone(timezone)
    base_utc = _coerce_now(now)
    base_local = base_utc.astimezone(ZoneInfo(tz_name))
    next_local = croniter(cron_expr, base_local).get_next(datetime)
    return next_local.astimezone(UTC).timestamp()


class CronJobCreate(BaseModel):
    thread_id: str
    assistant_id: str | None = None
    cron: str
    timezone: str
    enabled: bool = True
    input: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] | None = None
    context: dict[str, Any] | None = None
    multitask_strategy: Literal["reject", "interrupt", "rollback", "enqueue"] = "enqueue"

    @field_validator("cron")
    @classmethod
    def _validate_cron(cls, value: str) -> str:
        return _validate_cron(value)

    @field_validator("timezone")
    @classmethod
    def _validate_timezone_name(cls, value: str) -> str:
        return _validate_timezone(value)


class CronJobRecord(CronJobCreate):
    job_id: str
    next_fire_at: float | None = None
    last_fire_at: float | None = None
    last_run_id: str | None = None
    created_at: float
    updated_at: float
