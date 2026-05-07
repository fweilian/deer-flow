# ruff: noqa: I001
"""Shared utility functions for the Postgres checkpoint & storage classes."""

from collections.abc import Iterator
from contextlib import contextmanager

from deerflow.runtime.gaussdb_compat import Connection
from deerflow.runtime.gaussdb_compat.rows import DictRow
from deerflow.runtime.gaussdb_compat.pool import ConnectionPool

Conn = Connection[DictRow] | ConnectionPool[Connection[DictRow]]


@contextmanager
def get_connection(conn: Conn) -> Iterator[Connection[DictRow]]:
    if isinstance(conn, Connection):
        yield conn
    elif isinstance(conn, ConnectionPool):
        with conn.connection() as conn:
            yield conn
    else:
        raise TypeError(f"Invalid connection type: {type(conn)}")
