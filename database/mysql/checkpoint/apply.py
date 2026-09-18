#!/usr/bin/env python3
"""Apply the pinned MySQL checkpoint artifact as a DBA-owned operation.

This script is intentionally outside the Gateway/runtime package. It reads the
version table before every ordered file, so a completed migration is skipped
and a gap fails rather than replaying non-idempotent DDL.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def _connection_options(url: str) -> dict[str, object]:
    from langgraph.checkpoint.mysql.asyncmy import AsyncMySaver

    # The upstream parser owns the accepted URL shape; the resulting keyword
    # names are shared by asyncmy and PyMySQL (``db``, ``user``, etc.).
    return AsyncMySaver.parse_conn_string(url)


def _current_version(cursor) -> int:
    try:
        cursor.execute("SELECT MAX(v) FROM checkpoint_migrations")
    except Exception as exc:
        if getattr(exc, "args", [None])[0] == 1146:  # ER_NO_SUCH_TABLE
            return -1
        raise
    row = cursor.fetchone()
    return -1 if row is None or row[0] is None else int(row[0])


def _split_migration(path: Path) -> tuple[str, str]:
    statement, marker = path.read_text(encoding="utf-8").rsplit("INSERT INTO checkpoint_migrations", 1)
    return statement.strip(), f"INSERT INTO checkpoint_migrations{marker}".strip()


def apply(url: str) -> None:
    import pymysql

    artifact_dir = Path(__file__).resolve().parent
    migrations = sorted(artifact_dir.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    connection = pymysql.connect(autocommit=False, **_connection_options(url))
    try:
        with connection.cursor() as cursor:
            for version, path in enumerate(migrations):
                current = _current_version(cursor)
                if current >= version:
                    continue
                if current != version - 1:
                    raise RuntimeError(
                        f"checkpoint migration gap: database is at {current}, cannot apply {path.name}"
                    )
                statement, marker = _split_migration(path)
                cursor.execute(statement)
                cursor.execute(marker)
                connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply DeerFlow's pinned MySQL checkpoint migrations")
    parser.add_argument("--url", required=True, help="MySQL URL for the DBA-owned migration account")
    apply(parser.parse_args().url)


if __name__ == "__main__":
    main()
