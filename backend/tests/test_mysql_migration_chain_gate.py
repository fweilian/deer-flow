"""Compatibility gate for the future independent MySQL Alembic chain.

This is intentionally an isolated migration tree.  Goal 1 proves the chain
selection and isolation contract before Goal 2 freezes the real application
baseline; it must not create a partial production schema merely to obtain a
revision id.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url

from deerflow.persistence import bootstrap

_ENV_PY = """\
from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

if context.is_offline_mode():
    context.configure(url=config.get_main_option(\"sqlalchemy.url\"), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = engine_from_config(config.get_section(config.config_ini_section), prefix=\"sqlalchemy.\", poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
"""

_BASELINE = """\
revision = \"0001_mysql_baseline\"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    pass

def downgrade():
    pass
"""


def _write_mysql_tree(path: Path) -> None:
    (path / "versions").mkdir(parents=True)
    (path / "env.py").write_text(_ENV_PY, encoding="utf-8")
    (path / "versions" / "0001_mysql_baseline.py").write_text(_BASELINE, encoding="utf-8")


def test_mysql_chain_is_isolated_from_immutable_postgres_history(monkeypatch, tmp_path: Path) -> None:
    mysql_tree = tmp_path / "migrations_mysql"
    _write_mysql_tree(mysql_tree)
    monkeypatch.setattr(bootstrap, "_MYSQL_MIGRATIONS_DIR", mysql_tree)

    # Selection is backend-explicit: the MySQL chain cannot discover or replay
    # the immutable PostgreSQL history.
    assert bootstrap._migration_script_location("postgres") == bootstrap._MIGRATIONS_DIR
    assert bootstrap._migration_script_location("mysql") == mysql_tree
    assert mysql_tree != bootstrap._MIGRATIONS_DIR

    pg_revisions = {path.stem for path in (bootstrap._MIGRATIONS_DIR / "versions").glob("*.py")}
    mysql_revisions = {path.stem for path in (mysql_tree / "versions").glob("*.py")}
    assert "0001_baseline" in pg_revisions
    assert "0023_user_preferences" in pg_revisions
    assert mysql_revisions == {"0001_mysql_baseline"}
    assert pg_revisions.isdisjoint(mysql_revisions)

    mysql_scripts = ScriptDirectory(str(mysql_tree))
    assert mysql_scripts.get_heads() == ["0001_mysql_baseline"]
    revision = mysql_scripts.get_revision("0001_mysql_baseline")
    assert revision is not None
    assert revision.down_revision is None
    assert bootstrap._get_head_revision(backend="mysql") == "0001_mysql_baseline"


def test_mysql_chain_can_be_upgraded_outside_runtime_and_detects_mismatch(monkeypatch, tmp_path: Path) -> None:
    mysql_tree = tmp_path / "migrations_mysql"
    _write_mysql_tree(mysql_tree)
    monkeypatch.setattr(bootstrap, "_MYSQL_MIGRATIONS_DIR", mysql_tree)

    database = tmp_path / "mysql-chain-proof.sqlite"
    engine = SimpleNamespace(url=make_url(f"sqlite:///{database}"))
    config = bootstrap._get_alembic_config(engine, backend="mysql")

    # This models the DBA / migration-process call.  Nothing invokes the
    # Gateway bootstrap path, and the default Alembic version table remains
    # ``alembic_version``.
    command.upgrade(config, "head")
    with create_engine(f"sqlite:///{database}").connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert revision == "0001_mysql_baseline"

        connection.execute(text("UPDATE alembic_version SET version_num = 'wrong_chain'"))
        connection.commit()
        mismatched = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()

    assert mismatched != bootstrap._get_head_revision(backend="mysql")
