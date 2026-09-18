"""Read-only MySQL schema checks used by the production runtime.

DDL is deliberately absent from this module.  DBA migration artifacts create
and upgrade schemas; runtime only verifies their result and fails closed.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

SCHEMA_ERROR = "Database schema is missing or outdated. Run the required database migration before starting this application version."
CHECKPOINT_TABLES = frozenset({"checkpoint_migrations", "checkpoints", "checkpoint_blobs", "checkpoint_writes"})


def required_checkpoint_migration_version() -> int:
    """Derive the required checkpoint schema version from the pinned saver."""
    from langgraph.checkpoint.mysql.base import MIGRATIONS

    return len(MIGRATIONS) - 1


async def _fetch_scalar(conn: Any, statement: str, params: tuple[object, ...] = ()) -> Any:
    async with conn.cursor() as cursor:
        await cursor.execute(statement, params)
        row = await cursor.fetchone()
    return None if row is None else row[0]


async def verify_application_revision(conn: Any, expected_revision: str) -> None:
    """Require exactly one Alembic revision row matching ``expected_revision``."""
    try:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT version_num FROM alembic_version")
            rows = await cursor.fetchall()
    except Exception as exc:
        raise RuntimeError(SCHEMA_ERROR) from exc
    if len(rows) != 1 or rows[0][0] != expected_revision:
        raise RuntimeError(SCHEMA_ERROR)


async def verify_checkpoint_schema(conn: Any) -> None:
    """Validate the four Saver-owned tables and pinned migration level."""
    try:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name IN (%s, %s, %s, %s)",
                tuple(sorted(CHECKPOINT_TABLES)),
            )
            tables: Iterable[tuple[str]] = await cursor.fetchall()
        if {row[0] for row in tables} != CHECKPOINT_TABLES:
            raise RuntimeError(SCHEMA_ERROR)
        version = await _fetch_scalar(conn, "SELECT MAX(v) FROM checkpoint_migrations")
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(SCHEMA_ERROR) from exc
    if version != required_checkpoint_migration_version():
        raise RuntimeError(SCHEMA_ERROR)
