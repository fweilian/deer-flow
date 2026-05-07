"""Minimal sync GaussDB connectivity probe."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import gaussdb


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
class SyncProbeResult:
    host: str
    port: int
    ok: bool
    message: str
    rows: list[Any] | None = None


@dataclass(slots=True)
class SyncGaussDBConnectionTester:
    nodes: list[tuple[str, int]] = field(default_factory=lambda: _parse_nodes(5432))
    user: str = os.getenv("GAUSSDB_USER", "omm")
    password: str = os.getenv("GAUSSDB_PASSWORD", "")
    database: str = os.getenv("GAUSSDB_DATABASE", "postgres")
    sslmode: str | None = os.getenv("GAUSSDB_SSLMODE")
    query: str = os.getenv("GAUSSDB_TEST_QUERY", "SELECT 1 AS ok")

    def _connect_kwargs(self, host: str, port: int) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "host": host,
            "port": port,
            "user": self.user,
            "password": self.password,
            "dbname": self.database,
        }
        if self.sslmode:
            kwargs["sslmode"] = self.sslmode
        return kwargs

    def ping_node(self, host: str, port: int) -> SyncProbeResult:
        conn = None
        try:
            conn = gaussdb.connect(**self._connect_kwargs(host, port))
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
                row = cursor.fetchone()
            return SyncProbeResult(host=host, port=port, ok=True, message=f"ping ok: {row}")
        except Exception as exc:  # noqa: BLE001
            return SyncProbeResult(host=host, port=port, ok=False, message=f"ping failed: {exc}")
        finally:
            if conn is not None:
                conn.close()

    def fetch_all_node(self, host: str, port: int, query: str | None = None) -> SyncProbeResult:
        conn = None
        sql = query or self.query
        try:
            conn = gaussdb.connect(**self._connect_kwargs(host, port))
            with conn.cursor() as cursor:
                cursor.execute(sql)
                rows = cursor.fetchall()
            return SyncProbeResult(host=host, port=port, ok=True, message="query ok", rows=rows)
        except Exception as exc:  # noqa: BLE001
            return SyncProbeResult(host=host, port=port, ok=False, message=f"query failed: {exc}")
        finally:
            if conn is not None:
                conn.close()

    def probe_node(self, host: str, port: int, query: str | None = None) -> SyncProbeResult:
        ping_result = self.ping_node(host, port)
        if not ping_result.ok:
            return ping_result
        query_result = self.fetch_all_node(host, port, query=query)
        if query_result.ok:
            query_result.message = f"{ping_result.message}; query ok"
        return query_result

    def probe_all(self, query: str | None = None) -> list[SyncProbeResult]:
        return [self.probe_node(host, port, query=query) for host, port in self.nodes]

    def run_smoke_test(self) -> None:
        for result in self.probe_all():
            prefix = f"[{result.host}:{result.port}]"
            print(f"{prefix} {result.message}")
            if result.ok:
                rows = result.rows or []
                print(f"{prefix} rows={len(rows)} result={rows}")


if __name__ == "__main__":
    SyncGaussDBConnectionTester().run_smoke_test()
