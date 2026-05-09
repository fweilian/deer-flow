from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest


def _load_sqlite_admin():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "sqlite_admin.py"
    spec = importlib.util.spec_from_file_location("sqlite_admin", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _create_sample_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1)")
        conn.execute("INSERT INTO items (name, enabled) VALUES (?, ?)", ("alpha", 1))
        conn.execute("INSERT INTO items (name, enabled) VALUES (?, ?)", ("beta", 1))
        conn.commit()
    finally:
        conn.close()


def _fetch_items(db_path: Path) -> list[tuple[int, str, int]]:
    conn = sqlite3.connect(db_path)
    try:
        return list(conn.execute("SELECT id, name, enabled FROM items ORDER BY id"))
    finally:
        conn.close()


def test_update_delete_and_clear_rows(tmp_path: Path):
    mod = _load_sqlite_admin()
    db_path = tmp_path / "deerflow.db"
    _create_sample_db(db_path)

    assert (
        mod.main(
            [
                "--db-path",
                str(db_path),
                "update",
                "--table",
                "items",
                "--where",
                "id=1",
                "--set",
                "name=updated",
                "--set",
                "enabled=false",
            ]
        )
        == 0
    )
    assert _fetch_items(db_path)[0] == (1, "updated", 0)

    assert (
        mod.main(
            [
                "--db-path",
                str(db_path),
                "delete",
                "--table",
                "items",
                "--where",
                "id=2",
            ]
        )
        == 0
    )
    assert _fetch_items(db_path) == [(1, "updated", 0)]

    with pytest.raises(SystemExit, match="Refusing to clear an entire table without --yes"):
        mod.main(["--db-path", str(db_path), "clear", "--table", "items"])

    assert mod.main(["--db-path", str(db_path), "clear", "--table", "items", "--yes"]) == 0

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
        conn.execute("INSERT INTO items (name, enabled) VALUES (?, ?)", ("gamma", 1))
        conn.commit()
        assert conn.execute("SELECT id FROM items").fetchone()[0] == 1
    finally:
        conn.close()


def test_dry_run_does_not_modify_rows(tmp_path: Path):
    mod = _load_sqlite_admin()
    db_path = tmp_path / "deerflow.db"
    _create_sample_db(db_path)

    assert (
        mod.main(
            [
                "--db-path",
                str(db_path),
                "--dry-run",
                "update",
                "--table",
                "items",
                "--where",
                "id=1",
                "--set",
                "name=preview",
            ]
        )
        == 0
    )
    assert _fetch_items(db_path)[0] == (1, "alpha", 1)
