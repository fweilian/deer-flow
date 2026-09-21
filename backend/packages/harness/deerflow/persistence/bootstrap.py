"""Schema boundaries for MySQL migrations and SQLite development.

MySQL is DeerFlow's only production relational backend.  Its frozen Alembic
artifact is applied by DBAs; Runtime only reads its revision.  SQLite remains a
local development/test convenience and creates its ORM tables directly.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from pathlib import Path
from typing import Any

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

_MYSQL_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations_mysql"
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


def _alembic_safe_url(engine: Any) -> str:
    """Render an engine URL safely for Alembic's ConfigParser-backed config."""
    return _escape_url_for_alembic(engine.url.render_as_string(hide_password=False))


def _migration_script_location(backend: str = "mysql") -> Path:
    """Return the sole DBA-owned migration tree.

    Accepting only MySQL keeps application Runtime from accidentally reviving a
    retired migration path for a development backend.
    """
    if backend != "mysql":
        raise ValueError(f"migration artifact: unsupported backend {backend!r}")
    return _MYSQL_MIGRATIONS_DIR


def _get_alembic_config(engine: Any, *, backend: str = "mysql") -> AlembicConfig:
    """Build the DBA migration config for the frozen MySQL baseline."""
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(_migration_script_location(backend)))
    url = _alembic_safe_url(engine)
    # Runtime uses asyncmy; DBA migration tooling deliberately uses PyMySQL.
    cfg.set_main_option("sqlalchemy.url", url.replace("mysql+asyncmy://", "mysql+pymysql://", 1))
    return cfg


def _get_head_revision(*, backend: str = "mysql") -> str:
    """Read the frozen MySQL migration head without a process-global cache."""
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(_migration_script_location(backend)))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    if head is None:
        raise RuntimeError("MySQL migration artifact has no head revision")
    return head


def _run_create_all_sync(sync_conn: Any) -> None:
    """Create SQLite's local development schema only."""
    import deerflow.persistence.models  # noqa: F401 - populate shared metadata
    from deerflow.persistence.base import Base

    Base.metadata.create_all(sync_conn)


async def bootstrap_sqlite_schema(engine: AsyncEngine) -> None:
    """Create missing SQLite development tables without Alembic or version rows."""
    async with _get_sqlite_local_lock(engine):
        async with engine.begin() as conn:
            await conn.run_sync(_run_create_all_sync)
    logger.info("SQLite development schema initialized")
