from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import BigInteger, Boolean, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base
from deerflow.persistence.datetime_compat import UTCDateTime


class ScheduledTaskRunRow(Base):
    __tablename__ = "scheduled_task_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), index=True)
    # NULL identifies legacy history or direct inserts without a parent task.
    occurrence_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # New occurrences start False; NULL preserves unknown legacy accounting.
    launch_accounted: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    thread_id: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scheduled_for: Mapped[datetime] = mapped_column(UTCDateTime())
    trigger: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=lambda: datetime.now(UTC))

    __table_args__ = (
        Index("uq_scheduled_task_run_occurrence_seq", "task_id", "occurrence_seq", unique=True),
        # ``list_queued_runs`` filters queued work and orders it by retry
        # fairness then FIFO.  This is a MySQL locking-correctness index, not
        # speculative query tuning: without it a queue claim can scan-lock the
        # occurrence table.
        Index("idx_scheduled_task_runs_status_created", "status", "attempt_count", "created_at", "id"),
        # At most one non-terminal (queued/launching/running) occurrence per
        # task. Queued occurrences are deliberately durable; ``launching`` is
        # a short lease-fenced claim used so multiple gateway instances cannot
        # launch the same row. Sibling of the ``runs`` table's
        # ``uq_runs_thread_active``
        # (PR #4003); that one keys on ``thread_id`` and does not cover the
        # default ``fresh_thread_per_run`` context (every dispatch gets a new
        # thread), which is why the scheduled-task run row needs its own guard.
        #
        # Must live in ORM ``__table_args__`` for the transitional
        # SQLite/PostgreSQL empty-DB bootstrap. MySQL receives the equivalent
        # generated-column key from its independent DBA-owned baseline.
        Index(
            "uq_scheduled_task_run_active",
            "task_id",
            unique=True,
            sqlite_where=text("status IN ('queued', 'launching', 'running')"),
            postgresql_where=text("status IN ('queued', 'launching', 'running')"),
        ).ddl_if(dialect=("sqlite", "postgresql")),
    )
