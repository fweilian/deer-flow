"""ORM model for deployment-level managed subagent definitions."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base
from deerflow.persistence.datetime_compat import UTCDateTime


class ManagedSubagentRow(Base):
    __tablename__ = "managed_subagents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    definition: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
