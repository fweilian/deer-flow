"""Shared PostgreSQL schema validation helpers for zero-DDL deployments."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

APP_TABLES = frozenset({"threads_meta", "runs", "run_events", "feedback", "users"})
APP_INDEXES = frozenset(
    {
        "ix_threads_meta_assistant_id",
        "ix_threads_meta_user_id",
        "ix_runs_thread_id",
        "ix_runs_user_id",
        "ix_runs_thread_status",
        "ix_run_events_user_id",
        "ix_events_thread_cat_seq",
        "ix_events_run",
        "ix_feedback_run_id",
        "ix_feedback_thread_id",
        "ix_feedback_user_id",
        "ix_users_email",
        "idx_users_oauth_identity",
    }
)
CHECKPOINT_TABLES = frozenset(
    {
        "checkpoint_migrations",
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
    }
)
STORE_TABLES = frozenset({"store_migrations", "store"})


def _missing(expected: Iterable[str], actual: Iterable[str]) -> list[str]:
    return sorted(set(expected) - set(actual))


def _raise_missing(kind: str, names: list[str]) -> None:
    if names:
        joined = ", ".join(names)
        raise RuntimeError(f"PostgreSQL schema validation failed: missing {kind}: {joined}")


async def validate_app_schema(conn: AsyncConnection) -> None:
    """Validate DeerFlow ORM tables and indexes without issuing DDL."""
    table_rows = await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"))
    existing_tables = {row[0] for row in table_rows}
    _raise_missing("tables", _missing(APP_TABLES, existing_tables))

    index_rows = await conn.execute(text("SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"))
    existing_indexes = {row[0] for row in index_rows}
    _raise_missing("indexes", _missing(APP_INDEXES, existing_indexes))


def validate_checkpoint_schema(conn, *, minimum_version: int) -> None:
    """Validate LangGraph checkpoint schema and migration version."""
    with conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
        existing_tables = {row[0] for row in cur.fetchall()}
        _raise_missing("tables", _missing(CHECKPOINT_TABLES, existing_tables))

        cur.execute("SELECT MAX(v) FROM checkpoint_migrations")
        version = cur.fetchone()[0]
        if version is None or int(version) < minimum_version:
            raise RuntimeError(f"PostgreSQL schema validation failed: checkpoint_migrations version {version!r} is below required {minimum_version}")


async def validate_checkpoint_schema_async(conn, *, minimum_version: int) -> None:
    """Async variant of :func:`validate_checkpoint_schema`."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
        existing_tables = {row[0] for row in await cur.fetchall()}
        _raise_missing("tables", _missing(CHECKPOINT_TABLES, existing_tables))

        await cur.execute("SELECT MAX(v) FROM checkpoint_migrations")
        version = (await cur.fetchone())[0]
        if version is None or int(version) < minimum_version:
            raise RuntimeError(f"PostgreSQL schema validation failed: checkpoint_migrations version {version!r} is below required {minimum_version}")


def validate_store_schema(conn, *, minimum_version: int) -> None:
    """Validate LangGraph store schema and migration version."""
    with conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
        existing_tables = {row[0] for row in cur.fetchall()}
        _raise_missing("tables", _missing(STORE_TABLES, existing_tables))

        cur.execute("SELECT MAX(v) FROM store_migrations")
        version = cur.fetchone()[0]
        if version is None or int(version) < minimum_version:
            raise RuntimeError(f"PostgreSQL schema validation failed: store_migrations version {version!r} is below required {minimum_version}")


async def validate_store_schema_async(conn, *, minimum_version: int) -> None:
    """Async variant of :func:`validate_store_schema`."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
        existing_tables = {row[0] for row in await cur.fetchall()}
        _raise_missing("tables", _missing(STORE_TABLES, existing_tables))

        await cur.execute("SELECT MAX(v) FROM store_migrations")
        version = (await cur.fetchone())[0]
        if version is None or int(version) < minimum_version:
            raise RuntimeError(f"PostgreSQL schema validation failed: store_migrations version {version!r} is below required {minimum_version}")
