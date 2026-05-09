"""Command-line maintenance tool for DeerFlow SQLite databases.

Usage examples:

    # Update a single row
    PYTHONPATH=. python scripts/sqlite_admin.py update \
        --table cron_jobs \
        --where "job_id='abc123'" \
        --set enabled=false

    # Delete a row
    PYTHONPATH=. python scripts/sqlite_admin.py delete \
        --table runs \
        --where "run_id='run-1'"

    # Clear a whole table (explicit confirmation required)
    PYTHONPATH=. python scripts/sqlite_admin.py clear \
        --table cron_job_fires \
        --yes

The tool only targets SQLite files. By default it uses the configured
DeerFlow SQLite path when available, then falls back to the repo-local
``.deer-flow/data/deerflow.db`` layout.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _resolve_default_db_path() -> Path:
    """Return the most likely DeerFlow SQLite database path."""
    try:
        from deerflow.config.app_config import get_app_config

        config = get_app_config()
        if config.database.backend == "sqlite":
            return Path(config.database.sqlite_path)
    except Exception:
        logger.debug("Falling back to repo-local sqlite path", exc_info=True)

    candidates = (
        Path(".deer-flow/data/deerflow.db"),
        Path("backend/.deer-flow/data/deerflow.db"),
        Path("../.deer-flow/data/deerflow.db"),
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return candidates[1].resolve()


def _quote_identifier(name: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(name):
        raise ValueError(f"Invalid identifier: {name!r}")
    return f'"{name}"'


def _parse_value(raw: str) -> Any:
    text = raw.strip()
    if not text:
        return ""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _adapt_sqlite_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def _parse_assignments(items: list[str]) -> dict[str, Any]:
    if not items:
        raise ValueError("At least one --set key=value assignment is required.")

    assignments: dict[str, Any] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"Invalid assignment {item!r}; expected key=value.")
        key, raw_value = item.split("=", 1)
        key = key.strip()
        if not _IDENTIFIER_RE.fullmatch(key):
            raise ValueError(f"Invalid column name in --set: {key!r}")
        assignments[key] = _adapt_sqlite_value(_parse_value(raw_value))
    return assignments


@dataclass(slots=True)
class OperationResult:
    table: str
    matched_rows: int
    affected_rows: int
    sql: str


class SqliteMaintenanceTool:
    """Small helper for targeted SQLite row maintenance."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        if not self.db_path.exists():
            raise FileNotFoundError(f"SQLite database not found: {self.db_path}")
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SqliteMaintenanceTool:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: D401, ANN001
        self.close()

    def tables(self) -> list[str]:
        rows = self._conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        return [row[0] for row in rows]

    def columns(self, table: str) -> set[str]:
        self._ensure_table_exists(table)
        rows = self._conn.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()
        return {row[1] for row in rows}

    def _ensure_table_exists(self, table: str) -> None:
        if not _IDENTIFIER_RE.fullmatch(table):
            raise ValueError(f"Invalid table name: {table!r}")
        exists = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? AND name NOT LIKE 'sqlite_%'",
            (table,),
        ).fetchone()
        if exists is None:
            known = ", ".join(self.tables()) or "<none>"
            raise ValueError(f"Unknown table {table!r}. Known tables: {known}")

    def _count_rows(self, table: str, where_sql: str | None = None) -> int:
        quoted_table = _quote_identifier(table)
        sql = f"SELECT COUNT(*) FROM {quoted_table}"
        if where_sql:
            sql += f" WHERE {where_sql}"
        row = self._conn.execute(sql).fetchone()
        return int(row[0] if row is not None else 0)

    def update(self, table: str, where_sql: str, assignments: dict[str, Any], *, dry_run: bool = False) -> OperationResult:
        self._ensure_table_exists(table)
        if not where_sql.strip():
            raise ValueError("--where cannot be empty for update.")

        table_columns = self.columns(table)
        unknown = sorted(set(assignments) - table_columns)
        if unknown:
            raise ValueError(f"Unknown column(s) for table {table!r}: {', '.join(unknown)}")

        set_sql = ", ".join(f"{_quote_identifier(column)} = :set_{index}" for index, column in enumerate(assignments))
        params = {f"set_{index}": value for index, value in enumerate(assignments.values())}
        quoted_table = _quote_identifier(table)
        sql = f"UPDATE {quoted_table} SET {set_sql} WHERE {where_sql}"
        matched = self._count_rows(table, where_sql)

        logger.info("Matched %d row(s) in %s", matched, table)
        logger.info("SQL: %s", sql)
        if dry_run:
            return OperationResult(table=table, matched_rows=matched, affected_rows=0, sql=sql)

        cursor = self._conn.execute(sql, params)
        self._conn.commit()
        affected = cursor.rowcount if cursor.rowcount is not None else matched
        return OperationResult(table=table, matched_rows=matched, affected_rows=affected, sql=sql)

    def delete(self, table: str, where_sql: str, *, dry_run: bool = False) -> OperationResult:
        self._ensure_table_exists(table)
        if not where_sql.strip():
            raise ValueError("--where cannot be empty for delete.")

        quoted_table = _quote_identifier(table)
        sql = f"DELETE FROM {quoted_table} WHERE {where_sql}"
        matched = self._count_rows(table, where_sql)

        logger.info("Matched %d row(s) in %s", matched, table)
        logger.info("SQL: %s", sql)
        if dry_run:
            return OperationResult(table=table, matched_rows=matched, affected_rows=0, sql=sql)

        cursor = self._conn.execute(sql)
        self._conn.commit()
        affected = cursor.rowcount if cursor.rowcount is not None else matched
        return OperationResult(table=table, matched_rows=matched, affected_rows=affected, sql=sql)

    def clear(self, table: str, *, dry_run: bool = False, reset_sequence: bool = True) -> OperationResult:
        self._ensure_table_exists(table)

        quoted_table = _quote_identifier(table)
        sql = f"DELETE FROM {quoted_table}"
        matched = self._count_rows(table)

        logger.warning("Clearing table %s with %d row(s)", table, matched)
        logger.info("SQL: %s", sql)
        if dry_run:
            return OperationResult(table=table, matched_rows=matched, affected_rows=0, sql=sql)

        cursor = self._conn.execute(sql)
        if reset_sequence:
            has_sequence = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'",
            ).fetchone()
            if has_sequence is not None:
                self._conn.execute(
                    "DELETE FROM sqlite_sequence WHERE name = ?",
                    (table,),
                )
        self._conn.commit()
        affected = cursor.rowcount if cursor.rowcount is not None else matched
        return OperationResult(table=table, matched_rows=matched, affected_rows=affected, sql=sql)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Targeted SQLite maintenance tool for DeerFlow")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Path to the SQLite database file. Defaults to the configured DeerFlow SQLite path.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print SQL and row counts without modifying the database")

    subparsers = parser.add_subparsers(dest="command", required=True)

    update = subparsers.add_parser("update", help="Update rows in a table")
    update.add_argument("--table", required=True, help="Target table name")
    update.add_argument("--where", required=True, help="SQL WHERE clause used to select rows")
    update.add_argument(
        "--set",
        dest="set_items",
        action="append",
        required=True,
        metavar="COLUMN=VALUE",
        help="Column assignment to apply. May be repeated.",
    )

    delete = subparsers.add_parser("delete", help="Delete rows from a table")
    delete.add_argument("--table", required=True, help="Target table name")
    delete.add_argument("--where", required=True, help="SQL WHERE clause used to select rows")

    clear = subparsers.add_parser("clear", help="Delete all rows from a table")
    clear.add_argument("--table", required=True, help="Target table name")
    clear.add_argument("--yes", action="store_true", help="Confirm that the entire table should be deleted")
    clear.add_argument("--no-reset-sequence", action="store_true", help="Do not reset sqlite_sequence after clearing")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    db_path = args.db_path or _resolve_default_db_path()
    logger.info("Database: %s", db_path)
    logger.info("Dry run: %s", bool(args.dry_run))

    with SqliteMaintenanceTool(Path(db_path)) as tool:
        if args.command == "update":
            result = tool.update(args.table, args.where, _parse_assignments(args.set_items), dry_run=args.dry_run)
        elif args.command == "delete":
            result = tool.delete(args.table, args.where, dry_run=args.dry_run)
        elif args.command == "clear":
            if not args.yes:
                raise SystemExit("Refusing to clear an entire table without --yes")
            result = tool.clear(args.table, dry_run=args.dry_run, reset_sequence=not args.no_reset_sequence)
        else:  # pragma: no cover - argparse guarantees command is set
            raise SystemExit(f"Unknown command: {args.command}")

    logger.info(
        "Done: command=%s table=%s matched=%d affected=%d",
        args.command,
        result.table,
        result.matched_rows,
        result.affected_rows,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
