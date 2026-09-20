"""Regression coverage for the fresh-schema bootstrap boundary.

SQLite/PostgreSQL development starts may provision an empty database, but the
retired historical migration chain is no longer an executable upgrade path.
Existing unversioned schemas must fail closed without being stamped or mutated.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa

import deerflow.persistence.models  # noqa: F401  -- registers ORM models
from deerflow.persistence.base import Base
from deerflow.persistence.engine import close_engine, init_engine

pytestmark = pytest.mark.asyncio


def _seed_pre_3658_database(db_path: Path) -> None:
    """Build a DB that looks like a pre-PR-#3658 deployment.

    Uses the synchronous ``sqlite3`` driver so the seed is independent of the
    async engine under test.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Easiest way to get the legacy shape exactly right: create_all then
    # ALTER away the new column.
    sync_url = f"sqlite:///{db_path.as_posix()}"
    sync_engine = sa.create_engine(sync_url)
    try:
        Base.metadata.create_all(sync_engine)
        with sync_engine.begin() as conn:
            conn.execute(sa.text("ALTER TABLE runs DROP COLUMN token_usage_by_model"))
    finally:
        sync_engine.dispose()


async def test_legacy_database_is_rejected_without_schema_mutation(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    _seed_pre_3658_database(db_path)

    # Sanity: confirm we did indeed land in the buggy pre-fix shape before
    # init_engine touches the file.
    with sqlite3.connect(db_path) as raw:
        cols = {row[1] for row in raw.execute("PRAGMA table_info(runs)").fetchall()}
        assert "run_id" in cols
        assert "token_usage_by_model" not in cols
        version_table_count = raw.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='alembic_version'").fetchone()[0]
        assert version_table_count == 0

    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    try:
        with pytest.raises(RuntimeError, match="existing legacy schema is not an executable migration target"):
            await init_engine(backend="sqlite", url=url, sqlite_dir=str(tmp_path))
        with sqlite3.connect(db_path) as raw:
            cols = {row[1] for row in raw.execute("PRAGMA table_info(runs)").fetchall()}
            assert "token_usage_by_model" not in cols
            assert raw.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='alembic_version'").fetchone()[0] == 0
    finally:
        await close_engine()


async def test_unversioned_current_shape_is_rejected_without_being_stamped(tmp_path: Path) -> None:
    db_path = tmp_path / "manual_altered.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    sync_engine = sa.create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        Base.metadata.create_all(sync_engine)
        # Don't strip the column -- this is the "user already ran the
        # workaround" case where create_all already produced it.
    finally:
        sync_engine.dispose()

    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    try:
        with pytest.raises(RuntimeError, match="existing legacy schema is not an executable migration target"):
            await init_engine(backend="sqlite", url=url, sqlite_dir=str(tmp_path))
        with sqlite3.connect(db_path) as raw:
            cols = [row[1] for row in raw.execute("PRAGMA table_info(runs)").fetchall()]
            assert cols.count("token_usage_by_model") == 1
            assert raw.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='alembic_version'").fetchone()[0] == 0
    finally:
        await close_engine()
