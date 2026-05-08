"""Scheduler request/record schemas and cron timing helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator


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


class CronJobChannelTarget(BaseModel):
    channel_name: str
    chat_id: str
    thread_ts: str | None = None
    deerflow_thread_id: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class CronJobChannelDelivery(BaseModel):
    kind: Literal["channel"] = "channel"
    target_mode: Literal["local", "origin", "explicit"] = "explicit"
    channel_name: str | None = None
    chat_id: str | None = None
    thread_ts: str | None = None
    origin: CronJobChannelTarget | None = None
    options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("chat_id")
    @classmethod
    def _normalize_chat_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("channel_name")
    @classmethod
    def _normalize_channel_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("thread_ts")
    @classmethod
    def _normalize_thread_ts(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("options")
    @classmethod
    def _ensure_options(cls, value: dict[str, Any] | None) -> dict[str, Any]:
        return value or {}

    @field_validator("chat_id", mode="after")
    @classmethod
    def _validate_explicit_requirements(cls, value: str | None, info) -> str | None:
        mode = info.data.get("target_mode", "explicit")
        channel_name = info.data.get("channel_name")
        if mode == "explicit":
            if not channel_name:
                raise ValueError("Explicit delivery requires channel_name.")
            if not value:
                raise ValueError("Explicit delivery requires chat_id.")
        return value

    @model_validator(mode="after")
    def _validate_explicit_target(self) -> CronJobChannelDelivery:
        if self.target_mode == "explicit":
            if not self.channel_name:
                raise ValueError("Explicit delivery requires channel_name.")
            if not self.chat_id:
                raise ValueError("Explicit delivery requires chat_id.")
        return self


class CronApiRequestAuth(BaseModel):
    type: Literal["bearer", "header"] = Field(validation_alias=AliasChoices("type", "kind"))
    token: str | None = None
    name: str | None = None
    value: str | None = None

    @field_validator("token")
    @classmethod
    def _validate_bearer_token(cls, value: str | None, info) -> str | None:
        if info.data.get("type") == "bearer" and not value:
            raise ValueError("Bearer auth requires token.")
        return value

    @field_validator("value")
    @classmethod
    def _validate_header_auth(cls, value: str | None, info) -> str | None:
        if info.data.get("type") == "header":
            if not info.data.get("name"):
                raise ValueError("Header auth requires name.")
            if not value:
                raise ValueError("Header auth requires value.")
        return value


class CronApiRequest(BaseModel):
    method: Literal["POST", "PUT", "PATCH"] = "POST"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = 10.0
    body_template: dict[str, Any] | list[Any] | str | None = None
    auth: CronApiRequestAuth | None = None


class CronJobCreate(BaseModel):
    thread_id: str
    assistant_id: str | None = None
    creator_user_id: str = "default"
    cron: str
    timezone: str
    enabled: bool = True
    input: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] | None = None
    context: dict[str, Any] | None = None
    delivery: CronJobChannelDelivery | None = None
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


class CronJobFireRecord(BaseModel):
    fire_id: str
    job_id: str
    scheduled_fire_at: float
    status: str
    claim_owner: str | None = None
    claim_token: str | None = None
    lease_until: float | None = None
    run_id: str | None = None
    error: str | None = None
    delivery_status: Literal["pending", "sent", "failed"] | None = None
    delivery_error: str | None = None
    delivery_attempted_at: float | None = None
