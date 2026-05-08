"""SQLAlchemy async dialect registration for async_gaussdb."""

from __future__ import annotations

import re

from sqlalchemy.dialects import registry
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_dbapi, PGDialect_asyncpg


class PGDialect_async_gaussdb(PGDialect_asyncpg):
    driver = "async_gaussdb"

    _POSTGRES_VERSION_RE = re.compile(
        r".*(?:PostgreSQL|EnterpriseDB) "
        r"(\d+)\.?(\d+)?(?:\.(\d+))?(?:\.\d+)?(?:devel|beta)?"
    )
    _GAUSSDB_VERSION_RE = re.compile(
        r".*GaussDB Kernel (\d+)\.(\d+)(?:\.(\d+))?",
        re.IGNORECASE,
    )

    @classmethod
    def import_dbapi(cls):
        return AsyncAdapt_asyncpg_dbapi(__import__("async_gaussdb"))

    @classmethod
    def _parse_server_version_info(cls, version_string: str) -> tuple[int, ...]:
        for regex in (cls._POSTGRES_VERSION_RE, cls._GAUSSDB_VERSION_RE):
            match = regex.match(version_string)
            if match:
                return tuple(int(part) for part in match.group(1, 2, 3) if part is not None)

        raise AssertionError(f"Could not determine version from string '{version_string}'")

    def _get_server_version_info(self, connection):
        version_string = connection.exec_driver_sql("select pg_catalog.version()").scalar()
        return self._parse_server_version_info(version_string)


def register_gaussdb_async_dialect() -> None:
    """Register the ``gaussdb+async_gaussdb`` dialect if it is not already known."""
    registry.register(
        "gaussdb.async_gaussdb",
        "deerflow.persistence.dialects.gaussdb",
        "PGDialect_async_gaussdb",
    )
