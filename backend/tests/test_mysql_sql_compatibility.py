"""Focused Goal 3 checks for MySQL application SQL compatibility.

Set ``TEST_MYSQL_URI`` to a disposable MySQL 8.0.24 database to run the
server-side checks.  The integration fixture owns its temporary tables and is
separate from the production Runtime's zero-DDL path.
"""

from __future__ import annotations

import ast
import os
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import JSON, Column, Computed, Integer, MetaData, String, Table, create_engine, select, text
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateTable

import deerflow.persistence.models  # noqa: F401 - populate Base metadata
from deerflow.persistence.base import Base
from deerflow.persistence.datetime_compat import UTCDateTime
from deerflow.persistence.json_compat import json_match
from deerflow.persistence.mysql_errors import is_mysql_deadlock, mysql_duplicate_key_name
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.scheduled_task_runs.sql import _MYSQL_CLAIM_QUEUED_RUN, ScheduledTaskRunRepository
from deerflow.persistence.user.model import UserPreferenceRow, UserRow
from deerflow.persistence.user.preferences import UserPreferencesRepository, _preference_upsert_statement


def test_mysql_datetime_columns_keep_microseconds_and_utc_boundary() -> None:
    datetime_columns = [column for table in Base.metadata.tables.values() for column in table.columns if isinstance(column.type, UTCDateTime)]
    assert len(datetime_columns) == 29
    for column in datetime_columns:
        rendered = str(column.type.compile(dialect=mysql.dialect())).upper()
        assert rendered == "DATETIME(6)"

    type_ = UTCDateTime()
    source = datetime(2026, 9, 18, 12, 30, 1, 123456, tzinfo=timezone(timedelta(hours=8)))
    bound = type_.process_bind_param(source, mysql.dialect())
    assert bound == datetime(2026, 9, 18, 4, 30, 1, 123456)
    assert type_.process_result_value(bound, mysql.dialect()) == bound.replace(tzinfo=UTC)


def test_mysql_json_server_default_is_an_expression() -> None:
    ddl = str(CreateTable(RunRow.__table__).compile(dialect=mysql.dialect()))
    assert "token_usage_by_model JSON NOT NULL DEFAULT ('{}')" in ddl


def test_mysql_queue_claim_uses_a_self_join_not_a_target_table_subquery() -> None:
    statement = str(_MYSQL_CLAIM_QUEUED_RUN.compile(dialect=mysql.dialect()))
    assert "UPDATE scheduled_task_runs AS candidate" in statement
    assert "LEFT JOIN scheduled_task_runs AS older" in statement
    assert "NOT EXISTS" not in statement


@pytest.mark.parametrize(
    ("value", "required_fragments"),
    [
        (None, ("JSON_TYPE", "= 'NULL'")),
        (True, ("= 'BOOLEAN'", "= 'true'")),
        (7, ("= 'INTEGER'", "CAST", "SIGNED")),
        (1.5, ("IN ('DOUBLE', 'INTEGER')", "CAST", "DOUBLE")),
        ("value", ("= 'STRING'", "JSON_UNQUOTE")),
    ],
)
def test_json_match_mysql_uses_native_json_and_uppercase_type_names(value, required_fragments) -> None:
    table = Table("threads_meta", MetaData(), Column("metadata_json", JSON))
    compiled = str(json_match(table.c.metadata_json, "deerflow_pinned", value).compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True}))
    if value is not None:
        assert "JSON_EXTRACT" in compiled
        assert "JSON_TYPE(JSON_EXTRACT" in compiled
    for fragment in required_fragments:
        assert fragment in compiled


def test_mysql_generated_column_definitions_preserve_active_row_semantics() -> None:
    metadata = MetaData()
    runs = Table(
        "runs",
        metadata,
        Column("thread_id", String(64), nullable=False),
        Column("status", String(20), nullable=False),
        Column("active_thread_id", String(64), Computed("IF(status IN ('pending', 'running'), thread_id, NULL)", persisted=True)),
    )
    scheduled = Table(
        "scheduled_task_runs",
        metadata,
        Column("task_id", String(64), nullable=False),
        Column("status", String(16), nullable=False),
        Column("active_task_id", String(64), Computed("IF(status IN ('queued', 'launching', 'running'), task_id, NULL)", persisted=True)),
    )
    for table, generated_name in ((runs, "active_thread_id"), (scheduled, "active_task_id")):
        ddl = str(CreateTable(table).compile(dialect=mysql.dialect())).upper()
        assert "GENERATED ALWAYS AS" in ddl
        assert generated_name.upper() in ddl
        assert "STORED" in ddl


def test_mysql_preference_statement_uses_on_duplicate_key_update() -> None:
    statement = _preference_upsert_statement("mysql", user_id="u", key="mode", value="pro")
    compiled = str(statement.compile(dialect=mysql.dialect()))
    assert "ON DUPLICATE KEY UPDATE" in compiled
    assert "ON CONFLICT" not in compiled


@pytest.mark.integration
@pytest.mark.anyio
async def test_mysql_production_repositories_preserve_preference_and_occurrence_contracts() -> None:
    """Exercise both previously divergent production branches on MySQL 8.0.24."""
    uri = os.environ.get("TEST_MYSQL_URI")
    if not uri:
        pytest.skip("TEST_MYSQL_URI is not set")
    from sqlalchemy.engine import make_url

    from deerflow.config.database_config import DatabaseConfig
    from deerflow.persistence.scheduled_task_runs.model import ScheduledTaskRunRow
    from deerflow.persistence.scheduled_tasks.model import ScheduledTaskRow
    from deerflow.persistence.scheduled_tasks.sql import ScheduledTaskRepository

    config = DatabaseConfig(backend="mysql", mysql_url=uri)
    database = f"g3_repositories_{uuid4().hex}"
    admin_engine = create_async_engine(config.app_sqlalchemy_url, pool_size=1, max_overflow=0)
    engine = None
    try:
        async with admin_engine.begin() as connection:
            assert str((await connection.execute(text("SELECT VERSION()"))).scalar_one()).startswith("8.0.24")
            await connection.execute(text(f"CREATE DATABASE `{database}`"))

        engine = create_async_engine(make_url(config.app_sqlalchemy_url).set(database=database), isolation_level="READ COMMITTED", pool_size=2, max_overflow=0)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            for table in (UserRow.__table__, UserPreferenceRow.__table__, ScheduledTaskRow.__table__, ScheduledTaskRunRow.__table__):
                await connection.run_sync(lambda sync, table=table: table.create(sync))

        async with sessions.begin() as session:
            session.add(UserRow(id="user", email="user@example.com"))
        preferences = UserPreferencesRepository(sessions)
        await preferences.patch("user", {"mode": "pro", "notification_enabled": False})
        await preferences.patch("user", {"mode": "flash"})
        assert await preferences.get("user") == {"mode": "flash", "notification_enabled": False}

        tasks = ScheduledTaskRepository(sessions)
        occurrences = ScheduledTaskRunRepository(sessions)
        original = await tasks.create(
            task_id="task",
            user_id="user",
            thread_id=None,
            context_mode="fresh_thread_per_run",
            assistant_id=None,
            title="Goal 3",
            prompt="p",
            schedule_type="cron",
            schedule_spec={"cron": "* * * * *"},
            timezone="UTC",
            next_run_at=None,
        )
        await occurrences.create(
            run_record_id="occurrence",
            task_id="task",
            thread_id="occurrence-thread",
            scheduled_for=datetime.now(UTC),
            trigger="manual",
            status="success",
        )
        current = await tasks.get("task", user_id="user")
        assert current is not None
        assert current["updated_at"] == original["updated_at"]
    finally:
        if engine is not None:
            await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f"DROP DATABASE IF EXISTS `{database}`"))
        await admin_engine.dispose()


def test_mysql_auth_error_mapping_uses_error_code_and_key_name() -> None:
    from types import SimpleNamespace

    from app.gateway.auth.repositories.sqlite import _is_email_violation, _is_oauth_identity_violation, _is_uniqueness_violation

    duplicate = type("Duplicate", (), {"args": (1062, "Duplicate entry 'github-1' for key 'users.idx_users_oauth_identity'")})()
    email_duplicate = type("Duplicate", (), {"args": (1062, "Duplicate entry 'user@example.com' for key 'users.ix_users_email'")})()
    deadlock = type("Deadlock", (), {"args": (1213, "Deadlock found when trying to get lock")})()
    assert mysql_duplicate_key_name(duplicate) == "users.idx_users_oauth_identity"
    assert _is_oauth_identity_violation(SimpleNamespace(orig=duplicate))
    assert _is_email_violation(SimpleNamespace(orig=email_duplicate))
    assert _is_uniqueness_violation(SimpleNamespace(orig=duplicate))
    assert is_mysql_deadlock(deadlock)


def test_production_sql_has_no_mysql_silent_returning_or_time_functions() -> None:
    root = Path(__file__).resolve().parents[1]
    sources = [root / "app", root / "packages" / "harness" / "deerflow"]
    violations: list[str] = []
    for source_root in sources:
        for path in source_root.rglob("*.py"):
            if "migrations" in path.parts or "tests" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                attr = func.attr if isinstance(func, ast.Attribute) else None
                if attr == "returning" or attr == "date_trunc":
                    violations.append(f"{path.relative_to(root)}:{node.lineno}:{attr}")
                if isinstance(func, ast.Name) and func.id == "extract":
                    violations.append(f"{path.relative_to(root)}:{node.lineno}:extract")
    assert not violations, "MySQL silently accepts unsupported SQLAlchemy constructs: " + ", ".join(violations)


@pytest.mark.integration
def test_mysql_server_enforces_generated_active_and_oauth_unique_semantics() -> None:
    uri = os.environ.get("TEST_MYSQL_URI")
    if not uri:
        pytest.skip("TEST_MYSQL_URI is not set")
    from deerflow.config.database_config import DatabaseConfig

    engine = create_engine(DatabaseConfig(backend="mysql", mysql_url=uri).app_sync_sqlalchemy_url)
    suffix = uuid4().hex
    runs = f"g3_runs_{suffix}"
    occurrences = f"g3_occurrences_{suffix}"
    users = f"g3_users_{suffix}"
    times = Table(
        f"g3_times_{suffix}",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("moment", UTCDateTime(), nullable=False),
        Column("payload", JSON, nullable=False),
    )
    try:
        with engine.begin() as connection:
            times.create(connection)
            source = datetime(2026, 9, 18, 12, 30, 1, 123456, tzinfo=timezone(timedelta(hours=8)))
            connection.execute(times.insert().values(id=1, moment=source, payload={"pinned": True}))
            stored = connection.execute(select(times.c.moment, times.c.payload)).one()
            assert stored.moment == datetime(2026, 9, 18, 4, 30, 1, 123456, tzinfo=UTC)
            assert stored.payload == {"pinned": True}
            run_ddl = (
                f"CREATE TABLE {runs} (id INT PRIMARY KEY AUTO_INCREMENT, "
                "thread_id VARCHAR(64) NOT NULL, status VARCHAR(20) NOT NULL, "
                "active_thread_id VARCHAR(64) GENERATED ALWAYS AS "
                "(IF(status IN ('pending','running'), thread_id, NULL)) STORED, "
                "UNIQUE KEY uq_active (active_thread_id))"
            )
            occurrence_ddl = (
                f"CREATE TABLE {occurrences} (id INT PRIMARY KEY AUTO_INCREMENT, "
                "task_id VARCHAR(64) NOT NULL, status VARCHAR(20) NOT NULL, "
                "active_task_id VARCHAR(64) GENERATED ALWAYS AS "
                "(IF(status IN ('queued','launching','running'), task_id, NULL)) STORED, "
                "UNIQUE KEY uq_active (active_task_id))"
            )
            connection.execute(text(run_ddl))
            connection.execute(text(occurrence_ddl))
            connection.execute(text(f"CREATE TABLE {users} (id INT PRIMARY KEY AUTO_INCREMENT, oauth_provider VARCHAR(32) NULL, oauth_id VARCHAR(128) NULL, UNIQUE KEY idx_users_oauth_identity (oauth_provider, oauth_id))"))
            connection.execute(text(f"INSERT INTO {runs} (thread_id, status) VALUES ('thread', 'success'), ('thread', 'error'), ('thread', 'pending')"))
            connection.execute(text(f"INSERT INTO {occurrences} (task_id, status) VALUES ('task', 'success'), ('task', 'queued')"))
            connection.execute(text(f"INSERT INTO {users} (oauth_provider, oauth_id) VALUES (NULL, NULL), (NULL, NULL), ('github', NULL), ('github', NULL), ('github', 'id-1')"))
            with pytest.raises(IntegrityError):
                connection.execute(text(f"INSERT INTO {runs} (thread_id, status) VALUES ('thread', 'running')"))
            with pytest.raises(IntegrityError):
                connection.execute(text(f"INSERT INTO {occurrences} (task_id, status) VALUES ('task', 'launching')"))
            with pytest.raises(IntegrityError):
                connection.execute(text(f"INSERT INTO {users} (oauth_provider, oauth_id) VALUES ('github', 'id-1')"))
    finally:
        with engine.begin() as connection:
            times.drop(connection, checkfirst=True)
            for table in (runs, occurrences, users):
                connection.execute(text(f"DROP TABLE IF EXISTS {table}"))
        engine.dispose()


@pytest.mark.integration
def test_mysql_server_executes_json_match_for_thread_metadata_queries() -> None:
    """Pin the JSON predicate used by thread search/order on actual MySQL."""
    uri = os.environ.get("TEST_MYSQL_URI")
    if not uri:
        pytest.skip("TEST_MYSQL_URI is not set")
    from deerflow.config.database_config import DatabaseConfig

    engine = create_engine(DatabaseConfig(backend="mysql", mysql_url=uri).app_sync_sqlalchemy_url)
    table = Table(
        f"g5_metadata_{uuid4().hex}",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("metadata_json", JSON, nullable=False),
    )
    try:
        with engine.begin() as connection:
            table.create(connection)
            connection.execute(table.insert(), [{"id": 1, "metadata_json": {"deerflow_pinned": True}}, {"id": 2, "metadata_json": {"deerflow_pinned": False}}])
            rows = connection.execute(select(table.c.id).where(json_match(table.c.metadata_json, "deerflow_pinned", True))).scalars().all()
            assert rows == [1]
    finally:
        with engine.begin() as connection:
            table.drop(connection, checkfirst=True)
        engine.dispose()


@pytest.mark.integration
def test_mysql_server_accepts_json_expression_default() -> None:
    uri = os.environ.get("TEST_MYSQL_URI")
    if not uri:
        pytest.skip("TEST_MYSQL_URI is not set")
    from deerflow.config.database_config import DatabaseConfig

    engine = create_engine(DatabaseConfig(backend="mysql", mysql_url=uri).app_sync_sqlalchemy_url)
    suffix = uuid4().hex
    defaults = f"g3_defaults_{suffix}"
    try:
        with engine.begin() as connection:
            connection.execute(text(f"CREATE TABLE {defaults} (id INT PRIMARY KEY, token_usage_by_model JSON NOT NULL DEFAULT ('{{}}'))"))
            connection.execute(text(f"INSERT INTO {defaults} (id) VALUES (1)"))
            assert connection.execute(text(f"SELECT JSON_TYPE(token_usage_by_model) FROM {defaults} WHERE id = 1")).scalar_one() == "OBJECT"
    finally:
        with engine.begin() as connection:
            connection.execute(text(f"DROP TABLE IF EXISTS {defaults}"))
        engine.dispose()


@pytest.mark.integration
@pytest.mark.anyio
async def test_mysql_repository_claim_uses_the_1093_safe_statement() -> None:
    uri = os.environ.get("TEST_MYSQL_URI")
    if not uri:
        pytest.skip("TEST_MYSQL_URI is not set")
    from sqlalchemy.engine import make_url

    from deerflow.config.database_config import DatabaseConfig

    config = DatabaseConfig(backend="mysql", mysql_url=uri)
    database = f"g3_claim_{uuid4().hex}"
    admin_engine = create_async_engine(config.app_sqlalchemy_url, pool_size=1, max_overflow=0)
    engine = None
    try:
        async with admin_engine.begin() as connection:
            assert str((await connection.execute(text("SELECT VERSION()"))).scalar_one()).startswith("8.0.24")
            await connection.execute(text(f"CREATE DATABASE `{database}`"))

        engine = create_async_engine(make_url(config.app_sqlalchemy_url).set(database=database), pool_size=1, max_overflow=0)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            # Use a disposable database rather than a temporary table: MySQL
            # cannot reopen a temporary table through a self-join, while the
            # production table is permanent.
            await connection.execute(
                text(
                    """
                    CREATE TABLE scheduled_task_runs (
                        id VARCHAR(64) PRIMARY KEY,
                        task_id VARCHAR(64) NOT NULL,
                        occurrence_seq BIGINT NULL,
                        launch_accounted BOOL NULL,
                        thread_id VARCHAR(64) NOT NULL,
                        run_id VARCHAR(64) NULL,
                        scheduled_for DATETIME(6) NOT NULL,
                        `trigger` VARCHAR(16) NOT NULL,
                        status VARCHAR(16) NOT NULL,
                        error TEXT NULL,
                        lease_owner VARCHAR(128) NULL,
                        lease_expires_at DATETIME(6) NULL,
                        attempt_count INT NOT NULL DEFAULT 0,
                        started_at DATETIME(6) NULL,
                        finished_at DATETIME(6) NULL,
                        created_at DATETIME(6) NOT NULL
                    )
                    """
                )
            )
            await connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(64) PRIMARY KEY)"))
            await connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('0001_mysql_baseline')"))
            await connection.execute(
                text(
                    """
                    INSERT INTO scheduled_task_runs
                        (id, task_id, thread_id, scheduled_for, `trigger`, status, created_at)
                    VALUES
                        ('older', 'task-older', 'shared-thread', '2026-09-18 11:59:59', 'scheduled', 'queued', '2026-09-18 11:59:59'),
                        ('newer', 'task-newer', 'shared-thread', '2026-09-18 12:00:00', 'scheduled', 'queued', '2026-09-18 12:00:00')
                    """
                )
            )

        repository = ScheduledTaskRunRepository(sessions)
        now = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
        assert await repository.claim_queued_run("newer", lease_owner="worker", now=now, lease_seconds=60, global_max_concurrent_runs=2) is None
        claimed = await repository.claim_queued_run("older", lease_owner="worker", now=now, lease_seconds=60, global_max_concurrent_runs=2)
        assert claimed is not None
        assert claimed["status"] == "launching"
        assert claimed["attempt_count"] == 1
    finally:
        if engine is not None:
            await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f"DROP DATABASE IF EXISTS `{database}`"))
        await admin_engine.dispose()


@pytest.mark.anyio
async def test_returning_replacements_preserve_repository_contracts(tmp_path) -> None:
    """Exercise the four replacements without Runtime schema bootstrap."""
    from deerflow.persistence.run.model import RunRow
    from deerflow.persistence.run.sql import RunRepository
    from deerflow.persistence.scheduled_task_runs.model import ScheduledTaskRunRow
    from deerflow.persistence.scheduled_task_runs.sql import ScheduledTaskRunRepository
    from deerflow.persistence.scheduled_tasks.model import ScheduledTaskRow
    from deerflow.persistence.scheduled_tasks.sql import ScheduledTaskRepository

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/g3.db")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: RunRow.__table__.create(sync))
            await connection.run_sync(lambda sync: ScheduledTaskRow.__table__.create(sync))
            await connection.run_sync(lambda sync: ScheduledTaskRunRow.__table__.create(sync))

        runs = RunRepository(sessions)
        lease = datetime.now(UTC) + timedelta(minutes=5)
        await runs.put("g3-cancelled", thread_id="g3-thread", user_id="g3-user", owner_worker_id="worker", lease_expires_at=lease.isoformat())
        assert await runs.request_cancel("g3-cancelled", action="rollback") == "rollback"
        renewed = await runs.renew_lease("g3-cancelled", owner_worker_id="worker", lease_expires_at=(lease + timedelta(minutes=1)).isoformat())
        assert renewed.renewed and renewed.cancel_action == "rollback"
        assert not (await runs.finalize_if_not_cancelled("g3-cancelled", status="success")).finalized

        await runs.put("g3-complete", thread_id="g3-thread-2", user_id="g3-user", owner_worker_id="worker", lease_expires_at=lease.isoformat())
        assert (await runs.finalize_if_not_cancelled("g3-complete", status="success")).finalized

        tasks = ScheduledTaskRepository(sessions)
        occurrences = ScheduledTaskRunRepository(sessions)
        await tasks.create(
            task_id="g3-task",
            user_id="g3-user",
            thread_id=None,
            context_mode="fresh_thread_per_run",
            assistant_id=None,
            title="Goal 3",
            prompt="p",
            schedule_type="cron",
            schedule_spec={"cron": "* * * * *"},
            timezone="UTC",
            next_run_at=None,
        )
        occurrence = await occurrences.create(
            run_record_id="g3-occurrence",
            task_id="g3-task",
            thread_id="g3-occurrence-thread",
            scheduled_for=datetime.now(UTC),
            trigger="manual",
            status="success",
        )
        assert occurrence["id"] == "g3-occurrence"
        async with sessions() as session:
            assert await session.scalar(select(ScheduledTaskRunRow.occurrence_seq).where(ScheduledTaskRunRow.id == "g3-occurrence")) == 1
            assert await session.scalar(select(ScheduledTaskRow.last_occurrence_seq).where(ScheduledTaskRow.id == "g3-task")) == 1
    finally:
        await engine.dispose()
