#!/usr/bin/env python3
"""Run the DBA-owned MySQL application Alembic chain outside Gateway."""

from __future__ import annotations

import argparse
from types import SimpleNamespace

from alembic import command
from sqlalchemy.engine.url import make_url

from deerflow.persistence.bootstrap import _get_alembic_config


def _migration_url(raw_url: str):
    """Return the synchronous PyMySQL URL used by the DBA runner."""
    url = make_url(raw_url)
    if url.drivername in {"mysql", "mysql+asyncmy"}:
        return url.set(drivername="mysql+pymysql")
    return url


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply DeerFlow's MySQL application migrations")
    parser.add_argument("--url", required=True, help="MySQL URL for the DBA-owned migration account")
    args = parser.parse_args()
    # Migration is synchronous and deliberately uses PyMySQL.  The operator
    # facing examples accept the neutral ``mysql://`` form as well as the
    # Runtime's asyncmy URL, so normalize both before Alembic constructs its
    # engine rather than accidentally selecting SQLAlchemy's MySQLdb dialect.
    config = _get_alembic_config(SimpleNamespace(url=_migration_url(args.url)), backend="mysql")
    command.upgrade(config, "head")


if __name__ == "__main__":
    main()
