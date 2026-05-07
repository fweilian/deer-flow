"""Compatibility exports for connection pools."""

from gaussdb_pool import AsyncConnectionPool, ConnectionPool

__all__ = ["AsyncConnectionPool", "ConnectionPool"]
