# ruff: noqa: I001
"""Shared async utility functions for the Postgres checkpoint & storage classes."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from deerflow.runtime.gaussdb_compat import AsyncConnection
from deerflow.runtime.gaussdb_compat.rows import DictRow
from deerflow.runtime.gaussdb_compat.pool import AsyncConnectionPool

Conn = AsyncConnection[DictRow] | AsyncConnectionPool[AsyncConnection[DictRow]]


@asynccontextmanager
async def get_connection(
    conn: Conn,
) -> AsyncIterator[AsyncConnection[DictRow]]:
    if isinstance(conn, AsyncConnection):
        yield conn
    elif isinstance(conn, AsyncConnectionPool):
        async with conn.connection() as conn:
            yield conn
    else:
        raise TypeError(f"Invalid connection type: {type(conn)}")
