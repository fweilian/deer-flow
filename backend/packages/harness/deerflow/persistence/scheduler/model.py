"""ORM models for persisted cron scheduler state."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


class CronJobRow(Base):
    __tablename__ = "cron_jobs"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    assistant_id: Mapped[str | None] = mapped_column(String(128))
    cron_expr: Mapped[str] = mapped_column(String(128), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    input_json: Mapped[dict] = mapped_column(JSON, default=dict)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    config_json: Mapped[dict] = mapped_column(JSON, default=dict)
    context_json: Mapped[dict] = mapped_column(JSON, default=dict)
    multitask_strategy: Mapped[str] = mapped_column(String(20), default="enqueue", nullable=False)
    next_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_id: Mapped[str | None] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))


class CronJobFireRow(Base):
    __tablename__ = "cron_job_fires"
    __table_args__ = (UniqueConstraint("job_id", "scheduled_fire_at", name="uq_cron_job_fires_job_sched"),)

    fire_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("cron_jobs.job_id", ondelete="CASCADE"), nullable=False, index=True)
    scheduled_fire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="claimed")
    claim_owner: Mapped[str | None] = mapped_column(String(128))
    claim_token: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    run_id: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
