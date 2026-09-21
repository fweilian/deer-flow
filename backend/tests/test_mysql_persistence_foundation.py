"""Focused Goal 2 coverage for the MySQL persistence foundation."""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from deerflow.config.checkpointer_config import CheckpointerConfig
from deerflow.config.database_config import DatabaseConfig
from deerflow.persistence import engine as engine_mod
from deerflow.persistence.mysql_schema import (
    CHECKPOINT_TABLES,
    SCHEMA_ERROR,
    verify_application_revision,
    verify_checkpoint_schema,
)
from deerflow.runtime.checkpointer.provider import _resolve_checkpointer_config


class _Cursor:
    def __init__(self, responses: list[list[tuple[object, ...]]]) -> None:
        self._responses = responses
        self._current: list[tuple[object, ...]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, _statement: str, _params: tuple[object, ...] = ()) -> None:
        self._current = self._responses.pop(0)

    async def fetchall(self) -> list[tuple[object, ...]]:
        return self._current

    async def fetchone(self) -> tuple[object, ...] | None:
        return self._current[0] if self._current else None


class _Connection:
    def __init__(self, responses: list[list[tuple[object, ...]]]) -> None:
        self._cursor = _Cursor(responses)

    def cursor(self) -> _Cursor:
        return self._cursor


def test_mysql_config_generates_async_and_sync_urls() -> None:
    config = DatabaseConfig(backend="mysql", mysql_url="mysql://alice:secret@db.example:3307/deerflow")

    assert config.app_sqlalchemy_url == "mysql+asyncmy://alice:secret@db.example:3307/deerflow"
    assert config.app_sync_sqlalchemy_url == "mysql+pymysql://alice:secret@db.example:3307/deerflow"
    assert CheckpointerConfig(type="mysql", connection_string=config.mysql_url).type == "mysql"

    sync_url_config = DatabaseConfig(backend="mysql", mysql_url="mysql+pymysql://alice:secret@db.example/deerflow")
    assert sync_url_config.app_sqlalchemy_url == "mysql+asyncmy://alice:secret@db.example/deerflow"


def test_mysql_migrator_normalizes_neutral_and_async_runtime_urls() -> None:
    """DBA migration must never fall through to SQLAlchemy's MySQLdb dialect."""
    script = Path(__file__).resolve().parents[1] / "scripts" / "migrate_mysql.py"
    migration_url = runpy.run_path(str(script))["_migration_url"]
    assert migration_url("mysql://alice:secret@db.example/deerflow").drivername == "mysql+pymysql"
    assert migration_url("mysql+asyncmy://alice:secret@db.example/deerflow").drivername == "mysql+pymysql"


def test_mysql_engine_kwargs_harden_the_pool_and_set_read_committed() -> None:
    kwargs = engine_mod._mysql_engine_kwargs(echo=False, pool_size=7, pool_recycle=123, command_timeout=9)

    assert kwargs["pool_size"] == 7
    assert kwargs["pool_pre_ping"] is True
    assert kwargs["pool_recycle"] == 123
    assert kwargs["isolation_level"] == "READ COMMITTED"
    assert kwargs["connect_args"] == {"connect_timeout": 9}


@pytest.mark.anyio
async def test_mysql_engine_initialization_never_calls_schema_bootstrap(monkeypatch) -> None:
    await engine_mod.close_engine()
    monkeypatch.setitem(sys.modules, "asyncmy", object())
    isolation = MagicMock()
    isolation.scalar_one.return_value = "READ-COMMITTED"
    revision = MagicMock()
    revision.all.return_value = [("0001_mysql_baseline",)]
    connection = MagicMock()
    connection.execute = AsyncMock(side_effect=[isolation, revision])
    connect_cm = AsyncMock()
    connect_cm.__aenter__.return_value = connection
    connect_cm.__aexit__.return_value = False
    engine = MagicMock()
    engine.connect.return_value = connect_cm
    engine.dispose = AsyncMock()
    sqlite_bootstrap = AsyncMock()
    monkeypatch.setattr(engine_mod, "create_async_engine", lambda *_args, **_kwargs: engine)
    monkeypatch.setattr("deerflow.persistence.bootstrap.bootstrap_sqlite_schema", sqlite_bootstrap)

    await engine_mod.init_engine("mysql", url="mysql+asyncmy://user:pass@db/deerflow")

    sqlite_bootstrap.assert_not_awaited()
    await engine_mod.close_engine()


def test_unified_mysql_config_selects_async_only_checkpointer() -> None:
    config = type("AppConfig", (), {"database": DatabaseConfig(backend="mysql", mysql_url="mysql://user:pass@db/deerflow"), "checkpointer": None})()

    resolved = _resolve_checkpointer_config(config)

    assert resolved.type == "mysql"
    assert resolved.connection_string == "mysql://user:pass@db/deerflow"


@pytest.mark.integration
def test_mysql_managed_subagent_store_uses_the_shared_sql_backend() -> None:
    """Managed subagent CRUD must not retain a removed-backend store gate."""
    uri = os.environ.get("TEST_MYSQL_URI")
    if not uri:
        pytest.skip("TEST_MYSQL_URI is not set")

    from deerflow.persistence.managed_subagents import ManagedSubagentDefinition
    from deerflow.persistence.managed_subagents.sql import SqlManagedSubagentStore

    name = f"g5-managed-{uuid4().hex[:20]}"
    store = SqlManagedSubagentStore(DatabaseConfig(backend="mysql", mysql_url=uri).app_sync_sqlalchemy_url)
    definition = ManagedSubagentDefinition(name=name, description="G5 MySQL worker", system_prompt="Work carefully.")
    try:
        store.create(definition)
        assert store.get(name).description == "G5 MySQL worker"
        store.update(definition.model_copy(update={"enabled": False}))
        assert store.get(name).enabled is False
    finally:
        store.delete(name)


@pytest.mark.anyio
async def test_application_revision_verification_requires_one_expected_row() -> None:
    await verify_application_revision(_Connection([[("0001_mysql_baseline",)]]), "0001_mysql_baseline")

    with pytest.raises(RuntimeError, match=SCHEMA_ERROR):
        await verify_application_revision(_Connection([[("wrong",)]]), "0001_mysql_baseline")
    with pytest.raises(RuntimeError, match=SCHEMA_ERROR):
        await verify_application_revision(_Connection([[("a",), ("b",)]]), "a")


@pytest.mark.anyio
async def test_checkpoint_schema_verification_is_read_only_and_fail_closed(monkeypatch) -> None:
    # The default contributor/test install deliberately omits the optional
    # MySQL Saver dependency. Keep the verifier contract covered without
    # making the ordinary backend suite depend on the mysql extra; the
    # artifact test below separately checks the pinned upstream MIGRATIONS.
    monkeypatch.setattr("deerflow.persistence.mysql_schema.required_checkpoint_migration_version", lambda: 21)
    table_rows = [(name,) for name in CHECKPOINT_TABLES]
    await verify_checkpoint_schema(_Connection([table_rows, [(21,)]]))

    with pytest.raises(RuntimeError, match=SCHEMA_ERROR):
        await verify_checkpoint_schema(_Connection([table_rows[:-1], [(21,)]]))
    with pytest.raises(RuntimeError, match=SCHEMA_ERROR):
        await verify_checkpoint_schema(_Connection([table_rows, [(20,)]]))


def test_checkpoint_artifact_is_complete_and_derived_from_pinned_saver() -> None:
    pytest.importorskip("langgraph.checkpoint.mysql.base")
    from langgraph.checkpoint.mysql.base import MIGRATIONS

    artifact_dir = Path(__file__).resolve().parents[2] / "database/mysql/checkpoint"
    migrations = sorted(artifact_dir.glob("[0-9][0-9][0-9][0-9]_*.sql"))

    assert len(MIGRATIONS) == 22
    assert len(migrations) == len(MIGRATIONS)
    for version, (path, upstream) in enumerate(zip(migrations, MIGRATIONS, strict=True)):
        statement, marker = path.read_text(encoding="utf-8").rsplit("INSERT INTO checkpoint_migrations", 1)
        assert " ".join(statement.split()) == " ".join(upstream.split())
        assert f"VALUES ({version});" in marker
    readme = (artifact_dir / "README.md").read_text(encoding="utf-8")
    assert "SAVER_VERSION = 3.0.0" in readme
    assert "MIGRATIONS_COUNT = 22" in readme


@pytest.mark.integration
def test_mysql_final_baseline_migrates_an_empty_database() -> None:
    """The frozen root creates the complete application schema in one pass."""
    uri = os.environ.get("TEST_MYSQL_URI")
    if not uri:
        pytest.skip("TEST_MYSQL_URI is not set")

    from alembic import command
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.engine import make_url

    import deerflow.persistence.models  # noqa: F401 - populate Base metadata
    from deerflow.persistence.base import Base
    from deerflow.persistence.bootstrap import _get_alembic_config

    root_url = make_url(DatabaseConfig(backend="mysql", mysql_url=uri).app_sync_sqlalchemy_url)
    database = f"g5_baseline_{uuid4().hex}"
    admin = create_engine(root_url)
    try:
        with admin.begin() as connection:
            assert str(connection.execute(text("SELECT VERSION()")).scalar_one()).startswith("8.0.24")
            connection.execute(text(f"CREATE DATABASE `{database}`"))

        target_url = root_url.set(database=database)
        command.upgrade(_get_alembic_config(type("Engine", (), {"url": target_url})(), backend="mysql"), "head")
        target = create_engine(target_url)
        try:
            inspector = inspect(target)
            assert set(inspector.get_table_names()) == {"alembic_version", *Base.metadata.tables}
            assert len(Base.metadata.tables) == 12
            assert sum(len(table.columns) for table in Base.metadata.tables.values()) == 139
            generated_columns = {
                "runs": {"active_thread_id"},
                "scheduled_task_runs": {"active_task_id"},
            }
            for table_name, table in Base.metadata.tables.items():
                reflected = {column["name"] for column in inspector.get_columns(table_name)}
                assert reflected == {column.name for column in table.columns} | generated_columns.get(table_name, set())
            with target.connect() as connection:
                assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0001_mysql_baseline"
            assert inspector.get_columns("runs")[-1]["name"] == "active_thread_id"
            assert inspector.get_columns("scheduled_task_runs")[-1]["name"] == "active_task_id"
            assert {index["name"] for index in inspector.get_indexes("runs")} >= {"uq_runs_thread_active"}
            assert {index["name"] for index in inspector.get_indexes("scheduled_task_runs")} >= {"uq_scheduled_task_run_active"}
        finally:
            target.dispose()
    finally:
        with admin.begin() as connection:
            connection.execute(text(f"DROP DATABASE IF EXISTS `{database}`"))
        admin.dispose()


@pytest.mark.integration
@pytest.mark.anyio
async def test_mysql_runtime_paths_use_preinitialized_schema_without_ddl() -> None:
    """Exercise the actual app engine, async pool Saver, and sync AgentStore.

    The disposable database must already contain the DBA-owned checkpoint
    artifact. This test creates only its isolated ``agents``/``alembic_version``
    fixtures and never calls a Saver setup method.
    """
    uri = os.environ.get("TEST_MYSQL_URI")
    if not uri:
        pytest.skip("TEST_MYSQL_URI is not set")

    from langgraph.checkpoint.base import empty_checkpoint
    from sqlalchemy import create_engine, text

    from app.gateway.health import DATABASE_OK, _probe_checkpointer_backend
    from deerflow.persistence.agents.model import AgentRow
    from deerflow.persistence.agents.sql import SqlAgentStore
    from deerflow.runtime.checkpointer.async_provider import _async_checkpointer_from_database

    config = DatabaseConfig(backend="mysql", mysql_url=uri, pool_size=2, pool_recycle=120, command_timeout=10)
    await engine_mod.init_engine_from_config(config)
    try:
        engine = engine_mod.get_engine()
        assert engine is not None
        async with engine.connect() as connection:
            assert (await connection.execute(text("SELECT @@transaction_isolation"))).scalar_one() == "READ-COMMITTED"
        # Borrowing again exercises SQLAlchemy's pool_pre_ping path.
        async with engine.connect() as connection:
            assert (await connection.execute(text("SELECT 1"))).scalar_one() == 1

        async with _async_checkpointer_from_database(config) as saver:
            checkpoint_config = {"configurable": {"thread_id": "goal2-provider-smoke", "checkpoint_ns": ""}}
            checkpoint = empty_checkpoint()
            checkpoint["channel_values"] = {"goal2": "smoke"}
            checkpoint["channel_versions"] = {"goal2": "00000000000000000000000000000001.0000000000000001"}
            await saver.aput(checkpoint_config, checkpoint, {}, {})
        assert await _probe_checkpointer_backend(CheckpointerConfig(type="mysql", connection_string=uri)) == DATABASE_OK

        sync_engine = create_engine(config.app_sync_sqlalchemy_url)
        try:
            AgentRow.__table__.drop(sync_engine, checkfirst=True)
            AgentRow.__table__.create(sync_engine)
            store = SqlAgentStore(config.app_sync_sqlalchemy_url)
            store.create("goal2", {"name": "goal2", "description": "MySQL AgentStore smoke"}, "soul", user_id="goal2-user")
            assert store.get("goal2", user_id="goal2-user").name == "goal2"
        finally:
            AgentRow.__table__.drop(sync_engine, checkfirst=True)
            sync_engine.dispose()
    finally:
        await engine_mod.close_engine()
