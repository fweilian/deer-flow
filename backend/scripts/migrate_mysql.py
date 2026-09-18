#!/usr/bin/env python3
"""Run the DBA-owned MySQL application Alembic chain outside Gateway."""

from __future__ import annotations

import argparse
from types import SimpleNamespace

from alembic import command
from sqlalchemy.engine.url import make_url

from deerflow.persistence.bootstrap import _get_alembic_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply DeerFlow's MySQL application migrations")
    parser.add_argument("--url", required=True, help="MySQL URL for the DBA-owned migration account")
    args = parser.parse_args()
    config = _get_alembic_config(SimpleNamespace(url=make_url(args.url)), backend="mysql")
    command.upgrade(config, "head")


if __name__ == "__main__":
    main()
