"""Async checkpointer factory.

Provides an **async context manager** for long-running async servers that need
proper resource cleanup.

Supported backends: memory, sqlite, mysql.

Usage (e.g. FastAPI lifespan)::

    from deerflow.runtime.checkpointer.async_provider import make_checkpointer

    async with make_checkpointer() as checkpointer:
        app.state.checkpointer = checkpointer  # InMemorySaver if not configured

For sync usage see :mod:`deerflow.runtime.checkpointer.provider`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from langgraph.types import Checkpointer

from deerflow.config.app_config import AppConfig, get_app_config
from deerflow.runtime.checkpointer.provider import (
    SQLITE_INSTALL,
)
from deerflow.runtime.sqlite_utils import ensure_sqlite_parent_dir, resolve_sqlite_conn_str

logger = logging.getLogger(__name__)


def _prepare_sqlite_checkpointer_path(raw: str) -> str:
    conn_str = resolve_sqlite_conn_str(raw)
    ensure_sqlite_parent_dir(conn_str)
    return conn_str


def _prepare_database_sqlite_checkpointer_path(db_config) -> str:
    conn_str = db_config.checkpointer_sqlite_path
    ensure_sqlite_parent_dir(conn_str)
    return conn_str


@contextlib.asynccontextmanager
async def _mysql_saver(
    conn_string: str,
    *,
    pool_size: int,
    pool_recycle: int,
    connect_timeout: float | None,
) -> AsyncIterator[Checkpointer]:
    """Yield a Saver over DeerFlow's own asyncmy pool, without DDL."""
    try:
        import asyncmy
        from langgraph.checkpoint.mysql.asyncmy import AsyncMySaver
    except ImportError as exc:
        raise ImportError("MySQL checkpointer requires deerflow-harness[mysql]") from exc
    if not conn_string:
        raise ValueError("MySQL checkpointer requires a connection string")

    options = AsyncMySaver.parse_conn_string(conn_string)
    pool_options = {
        "minsize": 1,
        "maxsize": pool_size,
        "autocommit": True,
        "pool_recycle": pool_recycle,
        **options,
    }
    if connect_timeout is not None:
        pool_options["connect_timeout"] = connect_timeout
    pool = await asyncmy.create_pool(
        **pool_options,
    )
    try:
        from deerflow.persistence.mysql_schema import verify_checkpoint_schema

        async with pool.acquire() as connection:
            await verify_checkpoint_schema(connection)
        yield AsyncMySaver(conn=pool)
    finally:
        pool.close()
        await pool.wait_closed()


# ---------------------------------------------------------------------------
# Async factory
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _async_checkpointer(config) -> AsyncIterator[Checkpointer]:
    """Async context manager that constructs and tears down a checkpointer."""
    if config.type == "memory":
        from langgraph.checkpoint.memory import InMemorySaver

        yield InMemorySaver()
        return

    if config.type == "sqlite":
        try:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        except ImportError as exc:
            raise ImportError(SQLITE_INSTALL) from exc

        conn_str = await asyncio.to_thread(_prepare_sqlite_checkpointer_path, config.connection_string or "store.db")
        async with AsyncSqliteSaver.from_conn_string(conn_str) as saver:
            yield saver
        return

    if config.type == "mysql":
        async with _mysql_saver(config.connection_string or "", pool_size=5, pool_recycle=300, connect_timeout=30) as saver:
            yield saver
        return

    raise ValueError(f"Unknown checkpointer type: {config.type!r}")


# ---------------------------------------------------------------------------
# Public async context manager
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _async_checkpointer_from_database(db_config) -> AsyncIterator[Checkpointer]:
    """Async context manager that constructs a checkpointer from unified DatabaseConfig."""
    if db_config.backend == "memory":
        from langgraph.checkpoint.memory import InMemorySaver

        yield InMemorySaver()
        return

    if db_config.backend == "sqlite":
        try:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        except ImportError as exc:
            raise ImportError(SQLITE_INSTALL) from exc

        conn_str = await asyncio.to_thread(_prepare_database_sqlite_checkpointer_path, db_config)
        async with AsyncSqliteSaver.from_conn_string(conn_str) as saver:
            yield saver
        return

    if db_config.backend == "mysql":
        if not db_config.mysql_url:
            raise ValueError("database.mysql_url is required for the mysql backend")
        async with _mysql_saver(
            db_config.mysql_url,
            pool_size=db_config.pool_size,
            pool_recycle=db_config.pool_recycle,
            connect_timeout=db_config.command_timeout,
        ) as saver:
            yield saver
        return

    raise ValueError(f"Unknown database backend: {db_config.backend!r}")


@contextlib.asynccontextmanager
async def _select_inner_checkpointer(app_config: AppConfig) -> AsyncIterator[Checkpointer]:
    """Yield the raw checkpointer selected by *app_config* (no delta-cache wrapping).

    Priority:
    1. Legacy ``checkpointer:`` config section (backward compatible)
    2. Unified ``database:`` config section
    3. Default InMemorySaver
    """
    # Legacy: standalone checkpointer config takes precedence
    if app_config.checkpointer is not None:
        async with _async_checkpointer(app_config.checkpointer) as saver:
            yield saver
            return

    # Unified database config
    db_config = getattr(app_config, "database", None)
    if db_config is not None and db_config.backend != "memory":
        async with _async_checkpointer_from_database(db_config) as saver:
            yield saver
            return

    # Default: in-memory
    from langgraph.checkpoint.memory import InMemorySaver

    yield InMemorySaver()


@contextlib.asynccontextmanager
async def make_checkpointer(app_config: AppConfig | None = None) -> AsyncIterator[Checkpointer]:
    """Async context manager that yields a checkpointer for the caller's lifetime.
    Resources are opened on enter and closed on exit -- no global state::

        async with make_checkpointer(app_config) as checkpointer:
            app.state.checkpointer = checkpointer

    Yields an ``InMemorySaver`` when no checkpointer is configured in *config.yaml*.

    Backend selection priority:
    1. Legacy ``checkpointer:`` config section (backward compatible)
    2. Unified ``database:`` config section
    3. Default InMemorySaver

    When the effective checkpoint channel mode is ``delta`` (the process-frozen
    mode wins, falling back to ``database.checkpoint_channel_mode``), the raw
    saver is wrapped in a :class:`CachedHistorySaver` backed by a history cache
    whose lifetime equals this context manager's.
    """
    from deerflow.runtime.checkpoint_mode import frozen_checkpoint_channel_mode

    if app_config is None:
        app_config = get_app_config()

    async with _select_inner_checkpointer(app_config) as saver:
        db_config = getattr(app_config, "database", None)
        mode = frozen_checkpoint_channel_mode() or (db_config.checkpoint_channel_mode if db_config is not None else "full")
        if mode == "delta":
            from deerflow.runtime.checkpoint_cache.provider import (
                checkpoint_cache_key_prefix,
                make_checkpoint_cache,
            )
            from deerflow.runtime.checkpointer.cached_saver import CachedHistorySaver

            async with make_checkpoint_cache(app_config, serde=saver.serde) as cache:
                yield CachedHistorySaver(saver, cache, key_prefix=checkpoint_cache_key_prefix(app_config))
        else:
            yield saver
