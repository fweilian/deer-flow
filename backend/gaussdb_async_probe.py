"""Minimal async GaussDB connectivity probe."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any

import async_gaussdb


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value else default


def _parse_nodes(default_port: int) -> list[tuple[str, int]]:
    raw = os.getenv("GAUSSDB_NODES", "").strip()
    if not raw:
        return [(os.getenv("GAUSSDB_HOST", "127.0.0.1"), _env_int("GAUSSDB_PORT", default_port))]

    nodes: list[tuple[str, int]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            host, port = item.rsplit(":", 1)
            nodes.append((host.strip(), int(port.strip())))
        else:
            nodes.append((item, default_port))
    return nodes


@dataclass(slots=True)
class AsyncProbeResult:
    host: str
    port: int
    ok: bool
    message: str
    rows: list[Any] | None = None


@dataclass(slots=True)
class AsyncGaussDBConnectionTester:
    nodes: list[tuple[str, int]] = field(default_factory=lambda: _parse_nodes(5432))
    user: str = os.getenv("GAUSSDB_USER", "omm")
    password: str = os.getenv("GAUSSDB_PASSWORD", "")
    database: str = os.getenv("GAUSSDB_DATABASE", "postgres")
    query: str = os.getenv("GAUSSDB_TEST_QUERY", "SELECT 1 AS ok")

    def _connect_kwargs(self, host: str, port: int) -> dict[str, Any]:
        return {
            "host": host,
            "port": port,
            "user": self.user,
            "password": self.password,
            "database": self.database,
        }

    async def ping_node(self, host: str, port: int) -> AsyncProbeResult:
        conn = None
        try:
            conn = await async_gaussdb.connect(**self._connect_kwargs(host, port))
            row = await conn.fetchrow("SELECT 1")
            return AsyncProbeResult(host=host, port=port, ok=True, message=f"ping ok: {row}")
        except Exception as exc:  # noqa: BLE001
            return AsyncProbeResult(host=host, port=port, ok=False, message=f"ping failed: {exc}")
        finally:
            if conn is not None:
                await conn.close()

    async def fetch_all_node(self, host: str, port: int, query: str | None = None) -> AsyncProbeResult:
        conn = None
        sql = query or self.query
        try:
            conn = await async_gaussdb.connect(**self._connect_kwargs(host, port))
            rows = await conn.fetch(sql)
            return AsyncProbeResult(host=host, port=port, ok=True, message="query ok", rows=list(rows))
        except Exception as exc:  # noqa: BLE001
            return AsyncProbeResult(host=host, port=port, ok=False, message=f"query failed: {exc}")
        finally:
            if conn is not None:
                await conn.close()

    async def probe_node(self, host: str, port: int, query: str | None = None) -> AsyncProbeResult:
        ping_result = await self.ping_node(host, port)
        if not ping_result.ok:
            return ping_result
        query_result = await self.fetch_all_node(host, port, query=query)
        if query_result.ok:
            query_result.message = f"{ping_result.message}; query ok"
        return query_result

    async def probe_all(self, query: str | None = None) -> list[AsyncProbeResult]:
        tasks = [self.probe_node(host, port, query=query) for host, port in self.nodes]
        return await asyncio.gather(*tasks)

    async def run_smoke_test(self) -> None:
        for result in await self.probe_all():
            prefix = f"[{result.host}:{result.port}]"
            print(f"{prefix} {result.message}")
            if result.ok:
                rows = result.rows or []
                print(f"{prefix} rows={len(rows)} result={rows}")


if __name__ == "__main__":
    asyncio.run(AsyncGaussDBConnectionTester().run_smoke_test())
