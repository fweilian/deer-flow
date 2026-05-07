"""SQLAlchemy async dialect registration for async_gaussdb."""

from __future__ import annotations

from sqlalchemy.dialects import registry
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_dbapi, PGDialect_asyncpg


class PGDialect_async_gaussdb(PGDialect_asyncpg):
    driver = "async_gaussdb"

    @classmethod
    def import_dbapi(cls):
        return AsyncAdapt_asyncpg_dbapi(__import__("async_gaussdb"))


def register_gaussdb_async_dialect() -> None:
    """Register the ``gaussdb+async_gaussdb`` dialect if it is not already known."""
    registry.register(
        "gaussdb.async_gaussdb",
        "deerflow.persistence.dialects.gaussdb",
        "PGDialect_async_gaussdb",
    )
