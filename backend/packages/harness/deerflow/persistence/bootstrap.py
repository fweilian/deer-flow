"""Initialize the current DeerFlow application schema.

The PostgreSQL/Alembic migration chain remains an immutable historical record,
but is deliberately not an executable upgrade path. It creates feature tables
removed in Goal 0, so replaying it at Gateway startup would revive retired
runtime state. Fresh databases are created from the current ORM metadata and
stamped at the historical head. Existing schemas must be moved by the planned
fresh MySQL cutover, outside the application process.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

# The legacy PostgreSQL / SQLite chain is intentionally kept separate from
# the future MySQL chain.  ``_MIGRATIONS_DIR`` remains as a compatibility alias
# for focused historical-migration tests; new code must choose explicitly by
# backend through ``_migration_script_location``.
_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_MYSQL_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations_mysql"
_PG_LOCK_KEY = 0x0DEE_12F1_0BEE_3682
_SQLITE_LOCKS: weakref.WeakKeyDictionary[AsyncEngine, asyncio.Lock] = weakref.WeakKeyDictionary()


def _get_sqlite_local_lock(engine: AsyncEngine) -> asyncio.Lock:
    lock = _SQLITE_LOCKS.get(engine)
    if lock is None:
        lock = asyncio.Lock()
        _SQLITE_LOCKS[engine] = lock
    return lock


def _escape_url_for_alembic(url: str) -> str:
    """Escape ConfigParser interpolation characters in an SQLAlchemy URL."""
    return url.replace("%", "%%")


def _alembic_safe_url(engine: AsyncEngine) -> str:
    """Render an engine URL safely for Alembic's ConfigParser-backed config."""
    return _escape_url_for_alembic(engine.url.render_as_string(hide_password=False))


def _migration_script_location(backend: str) -> Path:
    """Return the independently-owned Alembic tree for *backend*.

    This deliberately stays as one explicit backend branch rather than a
    multi-chain registry.  The MySQL tree is populated by Goal 2; selecting it
    here must never make Alembic traverse the immutable PostgreSQL history.
    """
    if backend == "mysql":
        return _MYSQL_MIGRATIONS_DIR
    if backend in {"postgres", "sqlite"}:
        return _MIGRATIONS_DIR
    raise ValueError(f"bootstrap: unsupported migration backend {backend!r}")


def _get_alembic_config(engine: AsyncEngine, *, backend: str = "postgres", postgres_schema: str = "") -> AlembicConfig:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(_migration_script_location(backend)))
    cfg.set_main_option("sqlalchemy.url", _alembic_safe_url(engine))
    if postgres_schema:
        cfg.set_main_option("deerflow_pg_schema", postgres_schema)
    return cfg


def _get_head_revision(*, backend: str = "postgres") -> str:
    """Read the selected chain's current head without process-global cache."""
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(_migration_script_location(backend)))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    if head is None:
        raise RuntimeError("alembic has no head revision -- versions/ directory is empty")
    return head


async def _read_database_revision(conn: Any) -> str:
    result = await conn.execute(text("SELECT version_num FROM alembic_version"))
    rows = list(result.scalars())
    if len(rows) != 1 or not isinstance(rows[0], str) or not rows[0]:
        raise RuntimeError("bootstrap: expected one non-empty alembic_version row")
    return rows[0]


def _reflect_state(sync_conn: Any) -> dict[str, bool]:
    from deerflow.persistence.base import Base

    try:
        import deerflow.persistence.models  # noqa: F401
    except ImportError:
        logger.debug("deerflow.persistence.models not found; metadata may be incomplete")

    reflected = set(sa_inspect(sync_conn).get_table_names())
    return {
        "has_alembic_version": "alembic_version" in reflected,
        "has_deerflow_tables": bool(reflected & set(Base.metadata.tables)),
    }


def _decide_state(state: dict[str, bool]) -> str:
    if state["has_alembic_version"]:
        return "versioned"
    if state["has_deerflow_tables"]:
        return "legacy"
    return "empty"


def _run_create_all_sync(sync_conn: Any) -> None:
    from deerflow.persistence.base import Base

    try:
        import deerflow.persistence.models  # noqa: F401
    except ImportError:
        logger.debug("deerflow.persistence.models not found; bootstrap will create empty schema")
    Base.metadata.create_all(sync_conn)


def _stamp(cfg: AlembicConfig, revision: str) -> None:
    alembic_command.stamp(cfg, revision)


@asynccontextmanager
async def _postgres_lock(engine: AsyncEngine):
    """Serialise schema initialization across PostgreSQL Gateway processes."""
    async with engine.connect() as conn:
        await conn.execute(text("SET LOCAL idle_in_transaction_session_timeout = 0"))
        await conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _PG_LOCK_KEY})
        try:
            yield
        finally:
            try:
                await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _PG_LOCK_KEY})
            except Exception:  # noqa: BLE001
                logger.warning("bootstrap: pg_advisory_unlock raised; session close will release", exc_info=True)


@asynccontextmanager
async def _sqlite_lock(engine: AsyncEngine):
    """Serialise initialization within a SQLite Gateway process."""
    async with _get_sqlite_local_lock(engine):
        yield


def _bootstrap_lock(engine: AsyncEngine, *, backend: str):
    if backend == "postgres":
        return _postgres_lock(engine)
    if backend == "sqlite":
        return _sqlite_lock(engine)
    raise ValueError(f"bootstrap: unsupported backend {backend!r}")


async def bootstrap_schema(engine: AsyncEngine, *, backend: str, postgres_schema: str = "") -> None:
    """Create and stamp a fresh current schema, never replay legacy DDL."""
    head = await asyncio.to_thread(_get_head_revision, backend=backend)
    cfg = _get_alembic_config(engine, backend=backend, postgres_schema=postgres_schema if backend == "postgres" else "")

    async with _bootstrap_lock(engine, backend=backend):
        async with engine.connect() as conn:
            state = await conn.run_sync(_reflect_state)
            database_revision = await _read_database_revision(conn) if state["has_alembic_version"] else None
        decision = _decide_state(state)

        if decision == "empty":
            logger.info("bootstrap: fresh schema -> create_all + stamp head (%s)", head)
            async with engine.begin() as conn:
                await conn.run_sync(_run_create_all_sync)
            await asyncio.to_thread(_stamp, cfg, head)
        elif decision == "versioned" and database_revision == head:
            logger.info("bootstrap: current schema already stamped at head (%s)", head)
        else:
            raise RuntimeError("bootstrap: existing legacy schema is not an executable migration target; perform the fresh MySQL cutover before starting this Gateway")

    logger.info("bootstrap: complete (backend=%s)", backend)
