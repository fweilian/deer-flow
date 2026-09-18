"""Real-MySQL compatibility gate for ``langgraph-checkpoint-mysql==3.0.0``.

Set ``TEST_MYSQL_URI`` to a disposable MySQL 8.0.24 database.  The fixture
explicitly calls ``setup()`` because this is a DDL-capable integration test;
production Runtime code must never take this path.
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import pytest
from langgraph.checkpoint.base import empty_checkpoint

pytestmark = pytest.mark.integration


def _mysql_options(uri: str) -> dict[str, Any]:
    from langgraph.checkpoint.mysql.asyncmy import AsyncMySaver

    return AsyncMySaver.parse_conn_string(uri)


@asynccontextmanager
async def _open_mysql_saver() -> AsyncIterator[tuple[Any, Any]]:
    uri = os.environ.get("TEST_MYSQL_URI")
    if not uri:
        pytest.skip("TEST_MYSQL_URI is not set")

    import asyncmy
    from langgraph.checkpoint.mysql.asyncmy import AsyncMySaver

    pool = await asyncmy.create_pool(minsize=2, maxsize=4, autocommit=True, **_mysql_options(uri))
    try:
        # Construction happens while this async context is active.  This pins
        # the upstream get_running_loop() requirement and verifies the pool
        # duck-typed ``acquire()`` integration (not from_conn_string()).
        saver = AsyncMySaver(conn=pool)
        await saver.setup()
        yield saver, pool
    finally:
        pool.close()
        await pool.wait_closed()


def _config(thread_id: str, checkpoint_ns: str = "") -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns}}


def _checkpoint(*, channel: str, value: Any, version: int) -> dict[str, Any]:
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {channel: value}
    checkpoint["channel_versions"] = {channel: version}
    return checkpoint


@pytest.mark.anyio
async def test_asyncmy_pool_saver_preserves_checkpoint_api_and_mysql_schema() -> None:
    """Exercise DeerFlow's required async saver surface on a real pool."""
    thread_id = f"mysql-g1-{uuid4().hex}"
    namespace = "agent:child"
    payload = b"checkpoint-gate:" + (b"x" * (1024 * 1024))

    async with _open_mysql_saver() as (saver, pool):
        # Hold concurrent borrows long enough to prove the pool supplies
        # separate connections.  The saver below receives this same pool.
        async def connection_id() -> int:
            async with pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute("SELECT SLEEP(0.05), CONNECTION_ID()")
                    row = await cursor.fetchone()
                    return int(row[1])

        assert len(set(await asyncio.gather(*(connection_id() for _ in range(3))))) >= 2

        config = _config(thread_id, namespace)
        started = time.perf_counter()
        saved_config = await saver.aput(
            config,
            _checkpoint(channel="payload", value=payload, version=1),
            {"source": "loop", "step": 1, "writes": {}, "gate": "mysql-v5"},
            {"payload": 1},
        )
        write_ms = (time.perf_counter() - started) * 1000
        checkpoint_id = saved_config["configurable"]["checkpoint_id"]

        # Pending writes must survive the same tuple round trip used by
        # interrupt/retry/resume paths.  These are the canonical special
        # channels LangGraph assigns stable write indexes to.
        writes = [
            ("messages", {"kind": "pending"}),
            ("__interrupt__", {"kind": "interrupt"}),
            ("__error__", {"kind": "retry"}),
        ]
        await saver.aput_writes(saved_config, writes, task_id="gate-task")
        started = time.perf_counter()
        checkpoint_tuple = await saver.aget_tuple(saved_config)
        read_ms = (time.perf_counter() - started) * 1000
        assert checkpoint_tuple is not None
        assert checkpoint_tuple.config["configurable"]["checkpoint_id"] == checkpoint_id
        assert checkpoint_tuple.checkpoint["channel_values"]["payload"] == payload
        assert checkpoint_tuple.metadata["gate"] == "mysql-v5"
        assert {channel: value for task_id, channel, value in checkpoint_tuple.pending_writes if task_id == "gate-task"} == dict(writes)

        # This second checkpoint supplies the parent link used for resume.
        # A branch/regenerate-style copy is constructed the same way DeerFlow
        # does it: read an addressable tuple, then write it under another
        # thread rather than calling the optional copy_thread API.
        child_config = await saver.aput(
            saved_config,
            _checkpoint(channel="messages", value=["second turn"] * 64, version=2),
            {"source": "loop", "step": 2, "writes": {}, "gate": "mysql-v5"},
            {"messages": 2},
        )
        child_tuple = await saver.aget_tuple(child_config)
        assert child_tuple is not None
        assert child_tuple.parent_config is not None
        assert child_tuple.parent_config["configurable"]["checkpoint_id"] == checkpoint_id

        branch_thread_id = f"{thread_id}-branch"
        branched_checkpoint = dict(checkpoint_tuple.checkpoint)
        branched_checkpoint["id"] = empty_checkpoint()["id"]
        branch_config = await saver.aput(
            _config(branch_thread_id, namespace),
            branched_checkpoint,
            dict(checkpoint_tuple.metadata),
            dict(branched_checkpoint["channel_versions"]),
        )
        assert await saver.aget_tuple(branch_config) is not None

        # ``alist`` needs its normal history, filter, before, and limit modes.
        history = [item async for item in saver.alist(config, filter={"gate": "mysql-v5"}, limit=1)]
        assert [item.config["configurable"]["checkpoint_id"] for item in history] == [child_config["configurable"]["checkpoint_id"]]
        before = [item async for item in saver.alist(config, before=child_config, limit=1)]
        assert [item.config["configurable"]["checkpoint_id"] for item in before] == [checkpoint_id]

        # Separate namespaces and concurrent writes are both runtime-relevant:
        # the latter runs through the saver lock plus pool transaction path.
        async def write_parallel(index: int) -> None:
            parallel_config = _config(f"{thread_id}-parallel-{index}", namespace)
            await saver.aput(
                parallel_config,
                _checkpoint(channel="value", value={"index": index}, version=1),
                {"source": "loop", "step": index, "writes": {}},
                {"value": 1},
            )

        await asyncio.gather(*(write_parallel(index) for index in range(4)))

        async with pool.acquire() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT @@max_allowed_packet")
                max_allowed_packet = int((await cursor.fetchone())[0])
                await cursor.execute("SELECT DATA_TYPE, CHARACTER_MAXIMUM_LENGTH FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'checkpoint_blobs' AND column_name = 'blob'")
                blob_type, blob_limit = await cursor.fetchone()
                await cursor.execute(
                    "SELECT MAX(OCTET_LENGTH(`blob`)) FROM checkpoint_blobs WHERE thread_id = %s",
                    (thread_id,),
                )
                max_blob_size = int((await cursor.fetchone())[0])
                await cursor.execute(
                    "SELECT table_name, column_name, character_maximum_length "
                    "FROM information_schema.columns "
                    "WHERE table_schema = DATABASE() "
                    "AND (table_name, column_name) IN ("
                    "('checkpoints', 'thread_id'), ('checkpoints', 'checkpoint_id'), "
                    "('checkpoint_blobs', 'thread_id'), ('checkpoint_blobs', 'channel'), ('checkpoint_blobs', 'version'), "
                    "('checkpoint_writes', 'thread_id'), ('checkpoint_writes', 'checkpoint_id'), ('checkpoint_writes', 'task_id'))"
                )
                identifier_limits = {(table, column): int(limit) for table, column, limit in await cursor.fetchall()}

        assert blob_type.lower() == "longblob"
        assert int(blob_limit) == 4_294_967_295
        assert max_blob_size >= len(payload)
        assert all(limit == 150 for limit in identifier_limits.values())
        assert len(identifier_limits) == 8
        # JSON transport base64 is approximately 4/3 of the raw blob, plus a
        # small type prefix.  Keep a conservative measured packet margin.
        base64_transport_bytes = 4 * math.ceil(max_blob_size / 3)
        assert base64_transport_bytes < max_allowed_packet
        print(f"mysql V5 large-checkpoint evidence: blob={max_blob_size}B base64≈{base64_transport_bytes}B packet={max_allowed_packet}B write={write_ms:.2f}ms read={read_ms:.2f}ms")

        # The columns used by upstream INSERT IGNORE retain values inside
        # their schema limits for DeerFlow's real identifier shapes.
        assert len(thread_id) < 150
        assert len(checkpoint_id) < 150
        assert len("gate-task") < 150
        assert len("messages") < 150
        assert len("00000000000000000000000000000001.0000000000000001") < 150

        await saver.adelete_thread(thread_id)
        assert await saver.aget_tuple(config) is None
        await saver.adelete_thread(branch_thread_id)
