"""UTC-safe datetime mapping for the application ORM.

MySQL's ``DATETIME`` stores neither a timezone nor fractional seconds unless
an fsp is specified.  Application timestamps are always UTC, so the MySQL
boundary persists an aware UTC value as naive UTC and restores UTC on read.
This is deliberately a single focused mapping, not a general type framework.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy.dialects import mysql
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """Timestamp type with MySQL ``DATETIME(6)`` and UTC boundary semantics."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        if dialect.name == "mysql":
            return dialect.type_descriptor(mysql.DATETIME(fsp=6))
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if dialect.name == "mysql":
            if value.tzinfo is not None:
                return value.astimezone(UTC).replace(tzinfo=None)
            # Existing callers may pass parsed UTC ISO values without tzinfo.
            # Preserve that established contract while treating them as UTC.
            return value
        return value

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
