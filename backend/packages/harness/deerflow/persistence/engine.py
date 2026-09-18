"""Async SQLAlchemy engine lifecycle management.

Initializes at Gateway startup, provides session factory for
repositories, disposes at shutdown.

When database.backend="memory", init_engine is a no-op and
get_session_factory() returns None. Repositories must check for
None and fall back to in-memory implementations.
"""

from __future__ import annotations

import asyncio
import json
import logging

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

# Recycle pooled database connections before stale idle sockets can hang
# pool_pre_ping. The command timeout bounds stalled ORM queries independently.
POSTGRES_POOL_RECYCLE_SECONDS = 300
POSTGRES_COMMAND_TIMEOUT_SECONDS = 30


def _json_serializer(obj: object) -> str:
    """JSON serializer with ensure_ascii=False for Chinese character support."""
    return json.dumps(obj, ensure_ascii=False)


def _postgres_engine_kwargs(
    *,
    echo: bool,
    pool_size: int,
    pool_recycle: int = POSTGRES_POOL_RECYCLE_SECONDS,
    command_timeout: float | None = POSTGRES_COMMAND_TIMEOUT_SECONDS,
    connect_args: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build the shared SQLAlchemy engine options for PostgreSQL."""
    merged_connect_args = dict(connect_args or {})
    if command_timeout is not None:
        merged_connect_args["command_timeout"] = command_timeout
    return {
        "echo": echo,
        "pool_size": pool_size,
        "pool_pre_ping": True,
        "pool_recycle": pool_recycle,
        "connect_args": merged_connect_args,
        "json_serializer": _json_serializer,
    }


def _mysql_engine_kwargs(
    *,
    echo: bool,
    pool_size: int,
    pool_recycle: int = POSTGRES_POOL_RECYCLE_SECONDS,
    command_timeout: float | None = POSTGRES_COMMAND_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """Build SQLAlchemy options for the MySQL asyncmy application pool."""
    connect_args: dict[str, object] = {}
    if command_timeout is not None:
        connect_args["connect_timeout"] = command_timeout
    return {
        "echo": echo,
        "pool_size": pool_size,
        "pool_pre_ping": True,
        "pool_recycle": pool_recycle,
        "isolation_level": "READ COMMITTED",
        "connect_args": connect_args,
        "json_serializer": _json_serializer,
    }


def _mysql_pre_ping_dialect():
    """Return the smallest asyncmy-specific fix for SQLAlchemy pre-ping.

    SQLAlchemy 2.0.49's asyncmy adapter requires the positional ``reconnect``
    argument while its base ``do_ping`` calls ``ping()`` without one.  Passing
    ``False`` preserves pool_pre_ping's non-reconnecting probe semantics.
    """
    from sqlalchemy.dialects.mysql.asyncmy import MySQLDialect_asyncmy

    class _MySQLDialectWithPrePing(MySQLDialect_asyncmy):
        def do_ping(self, dbapi_connection) -> bool:
            dbapi_connection.ping(False)
            return True

    return _MySQLDialectWithPrePing()


logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def init_engine(
    backend: str,
    *,
    url: str = "",
    echo: bool = False,
    pool_size: int = 5,
    pool_recycle: int = POSTGRES_POOL_RECYCLE_SECONDS,
    command_timeout: float | None = POSTGRES_COMMAND_TIMEOUT_SECONDS,
    sqlite_dir: str = "",
    postgres_schema: str = "",
) -> None:
    """Create the async engine and session factory.

    Args:
        backend: "memory", "sqlite", "postgres", or "mysql".
        url: SQLAlchemy async URL (for sqlite/postgres/mysql).
        echo: Echo SQL to log.
        pool_size: Postgres connection pool size.
        pool_recycle: Seconds before Postgres connections are recycled.
        command_timeout: Timeout in seconds for app ORM Postgres commands, or None to disable.
        sqlite_dir: Directory to create for SQLite (ensured to exist).
        postgres_schema: Target PostgreSQL schema. Ignored for non-postgres.
    """
    global _engine, _session_factory

    if backend == "memory":
        logger.info("Persistence backend=memory -- ORM engine not initialized")
        return

    if backend == "postgres":
        try:
            import asyncpg  # noqa: F401
        except ImportError:
            raise ImportError(
                "database.backend is set to 'postgres' but asyncpg is not installed.\n"
                "Install it with:\n"
                "    cd backend && uv sync --all-packages --extra postgres\n"
                "On the next `make dev` the postgres extra is auto-detected from\n"
                "config.yaml (database.backend: postgres) and reinstalled, so it\n"
                "will not be wiped again. Set UV_EXTRAS=postgres in .env to opt in\n"
                "explicitly. Or switch to backend: sqlite in config.yaml for\n"
                "single-node deployment."
            ) from None

    if backend == "mysql":
        try:
            import asyncmy  # noqa: F401
        except ImportError:
            raise ImportError("database.backend is set to 'mysql' but asyncmy is not installed.\nInstall it with:\n    cd backend && uv sync --all-packages --extra mysql") from None

    if backend == "sqlite":
        import os

        from sqlalchemy import event

        # Offload the directory creation: ``init_engine`` runs on the FastAPI
        # lifespan event loop, and a sync ``os.makedirs`` (a stat + mkdir
        # syscall) blocks it during startup. Mirrors the #1912 fix for the
        # checkpointer's ``ensure_sqlite_parent_dir``.
        await asyncio.to_thread(os.makedirs, sqlite_dir or ".", exist_ok=True)
        _engine = create_async_engine(url, echo=echo, json_serializer=_json_serializer)

        # Enable WAL on every new connection. SQLite PRAGMA settings are
        # per-connection, so we wire the listener instead of running PRAGMA
        # once at startup. WAL gives concurrent reads + writers without
        # blocking and is the standard recommendation for any production
        # SQLite deployment (TC-UPG-06 in AUTH_TEST_PLAN.md). The companion
        # ``synchronous=NORMAL`` is the safe-and-fast pairing — fsync only
        # at WAL checkpoint boundaries instead of every commit.
        # We also widen ``busy_timeout`` to 30s here. Python's sqlite3 driver
        # defaults to 5s, which is fine for transient row contention but too
        # tight for cross-process bootstrap: the second-N-th Gateway process
        # may need to wait while the first runs ``ALTER TABLE`` /
        # ``CREATE TABLE`` for a fresh schema. The same widened timeout is
        # mirrored on the alembic-spawned engine in
        # ``migrations/env.py::run_migrations_online`` so its connections
        # behave identically.
        @event.listens_for(_engine.sync_engine, "connect")
        def _enable_sqlite_wal(dbapi_conn, _record):  # noqa: ARG001 — SQLAlchemy contract
            cursor = dbapi_conn.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL;")
                cursor.execute("PRAGMA synchronous=NORMAL;")
                cursor.execute("PRAGMA foreign_keys=ON;")
                cursor.execute("PRAGMA busy_timeout=30000;")
            finally:
                cursor.close()
    elif backend == "postgres":
        from deerflow.persistence.postgres_schema import build_asyncpg_connect_args

        pg_connect_args = build_asyncpg_connect_args(postgres_schema)
        _engine = create_async_engine(
            url,
            **_postgres_engine_kwargs(
                echo=echo,
                pool_size=pool_size,
                pool_recycle=pool_recycle,
                command_timeout=command_timeout,
                connect_args=pg_connect_args,
            ),
        )
    elif backend == "mysql":
        _engine = create_async_engine(
            url,
            dialect=_mysql_pre_ping_dialect(),
            **_mysql_engine_kwargs(
                echo=echo,
                pool_size=pool_size,
                pool_recycle=pool_recycle,
                command_timeout=command_timeout,
            ),
        )
    else:
        raise ValueError(f"Unknown persistence backend: {backend!r}")

    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)

    # Schema DDL belongs exclusively to DBA/migration and test fixtures.
    # Runtime owns connections only; a missing MySQL database fails closed
    # during startup rather than being silently auto-created.
    if backend == "mysql":
        from sqlalchemy import text

        async with _engine.connect() as conn:
            isolation = (await conn.execute(text("SELECT @@transaction_isolation"))).scalar_one()
        if str(isolation).upper() != "READ-COMMITTED":
            await _engine.dispose()
            _engine = None
            _session_factory = None
            raise RuntimeError(f"MySQL transaction isolation must be READ-COMMITTED; server reported {isolation!r}")
    logger.info("Persistence engine initialized without schema bootstrap: backend=%s", backend)


async def init_engine_from_config(config) -> None:
    """Convenience: init engine from a DatabaseConfig object."""
    if config.backend == "memory":
        await init_engine("memory")
        return
    await init_engine(
        backend=config.backend,
        url=config.app_sqlalchemy_url,
        echo=config.echo_sql,
        pool_size=config.pool_size,
        pool_recycle=config.pool_recycle,
        command_timeout=config.command_timeout,
        sqlite_dir=config.sqlite_dir if config.backend == "sqlite" else "",
        postgres_schema=config.postgres_schema if config.backend == "postgres" else "",
    )


def get_session_factory() -> async_sessionmaker[AsyncSession] | None:
    """Return the async session factory, or None if backend=memory."""
    return _session_factory


def get_engine() -> AsyncEngine | None:
    """Return the async engine, or None if not initialized."""
    return _engine


async def close_engine() -> None:
    """Dispose the engine, release all connections."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        logger.info("Persistence engine closed")
    _engine = None
    _session_factory = None
