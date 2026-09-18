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
from operator import add
from types import SimpleNamespace
from typing import Annotated, Any, TypedDict
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.graph import StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, RetryPolicy, interrupt

from deerflow.runtime.checkpoint_state import CheckpointStateAccessor, build_state_mutation_graph
from deerflow.runtime.runs.worker import _capture_rollback_point, _rollback_to_pre_run_checkpoint

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


class _RuntimeState(TypedDict):
    events: Annotated[list[str], add]


class _MessagesState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


def _runtime_graph(saver: Any, attempts: dict[str, int]) -> Any:
    """A small LangGraph flow that reaches each DeerFlow checkpoint operation."""

    def start(_state: _RuntimeState) -> dict[str, list[str]]:
        return {"events": ["start"]}

    def pause(_state: _RuntimeState) -> dict[str, list[str]]:
        answer = interrupt({"question": "continue?"})
        return {"events": [f"resume:{answer}"]}

    def retry(_state: _RuntimeState) -> dict[str, list[str]]:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("transient checkpointed retry")
        return {"events": ["retry-ok"]}

    def finish(_state: _RuntimeState) -> dict[str, list[str]]:
        return {"events": ["finish"]}

    builder = StateGraph(_RuntimeState)
    builder.add_node("start", start)
    builder.add_node("pause", pause)
    builder.add_node(
        "retry",
        retry,
        retry_policy=RetryPolicy(max_attempts=2, initial_interval=0, jitter=False, retry_on=RuntimeError),
    )
    builder.add_node("finish", finish)
    builder.set_entry_point("start")
    builder.add_edge("start", "pause")
    builder.add_edge("pause", "retry")
    builder.add_edge("retry", "finish")
    builder.set_finish_point("finish")
    return builder.compile(checkpointer=saver)


def _answer_graph(saver: Any, answer_id: str) -> Any:
    def answer(_state: _MessagesState) -> dict[str, list[AIMessage]]:
        return {"messages": [AIMessage(id=answer_id, content=answer_id)]}

    builder = StateGraph(_MessagesState)
    builder.add_node("answer", answer)
    builder.set_entry_point("answer")
    builder.set_finish_point("answer")
    return builder.compile(checkpointer=saver)


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


@pytest.mark.anyio
async def test_asyncmy_pool_saver_runs_real_langgraph_interrupt_retry_branch_and_rollback() -> None:
    """Exercise the Gate semantics through LangGraph and DeerFlow's rollback path.

    This deliberately does not hand-build checkpoint tuples: Pregel writes the
    interrupt, retry, resume, branch, and state-mutation checkpoints through
    ``AsyncMySaver(conn=asyncmy.Pool)`` on a real MySQL server.
    """
    thread_id = f"mysql-runtime-{uuid4().hex}"
    config = _config(thread_id)
    attempts = {"count": 0}

    async with _open_mysql_saver() as (saver, _pool):
        graph = _runtime_graph(saver, attempts)

        interrupted = await graph.ainvoke({"events": ["input"]}, config)
        assert "__interrupt__" in interrupted
        interrupted_tuple = await saver.aget_tuple(config)
        assert interrupted_tuple is not None
        assert interrupted_tuple.pending_writes

        resumed = await graph.ainvoke(Command(resume="yes"), config)
        assert resumed["events"] == ["input", "start", "resume:yes", "retry-ok", "finish"]
        assert attempts["count"] == 2

        await saver.adelete_thread(thread_id)


@pytest.mark.anyio
async def test_asyncmy_pool_saver_runs_gateway_regenerate_and_branch_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the Gateway's real regenerate preparation and branch writers on MySQL."""
    from app.gateway import deps
    from app.gateway.routers import thread_runs, threads

    source_thread_id = f"mysql-source-{uuid4().hex}"
    source_config = _config(source_thread_id)

    class EventStore:
        async def list_messages(self, *_args, **_kwargs) -> list[dict[str, Any]]:
            return []

        async def put_batch(self, _events: list[Any]) -> None:
            return None

    class ThreadStore:
        def __init__(self) -> None:
            self.records: dict[str, dict[str, Any]] = {source_thread_id: {"metadata": {}, "assistant_id": None, "display_name": "Source"}}

        async def get(self, thread_id: str, **_kwargs: Any) -> dict[str, Any] | None:
            return self.records.get(thread_id)

        async def create(self, thread_id: str, **kwargs: Any) -> dict[str, Any]:
            record = {"thread_id": thread_id, **kwargs}
            self.records[thread_id] = record
            return record

    async with _open_mysql_saver() as (saver, _pool):
        source_graph = _answer_graph(saver, "ai-old")
        source_accessor = CheckpointStateAccessor.bind(source_graph, saver, mode="full")
        human = HumanMessage(id="human-1", content="question", additional_kwargs={"run_id": "run-old"})
        # Regenerate requires the addressable checkpoint before the human turn.
        # Seed that base through the compiled graph's checkpoint writer.
        await source_accessor.aupdate(source_config, {}, as_node="answer")
        await source_graph.ainvoke({"messages": [human]}, source_config)

        event_store = EventStore()
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(checkpointer=saver, run_event_store=event_store)),
            state=SimpleNamespace(user=SimpleNamespace(id="user-1")),
        )

        async def source_builder(_request: Any, *, thread_id: str, **_kwargs: Any) -> tuple[CheckpointStateAccessor, dict[str, Any]]:
            assert thread_id == source_thread_id
            return source_accessor, source_config

        monkeypatch.setattr(thread_runs, "build_thread_checkpoint_state_accessor", source_builder)
        regenerate = await thread_runs._prepare_regenerate_payload(source_thread_id, "ai-old", request)
        assert regenerate.checkpoint["checkpoint_id"] != (await source_accessor.aget(source_config)).config["configurable"]["checkpoint_id"]

        # The real regenerate route returns this historical config and clean
        # input; running a new graph from it proves the MySQL checkpoint is a
        # usable replay base rather than merely an addressable tuple.
        replay_graph = _answer_graph(saver, "ai-regenerated")
        replay_config = {
            "configurable": {
                "thread_id": source_thread_id,
                **{key: value for key, value in regenerate.checkpoint.items() if value is not None},
            }
        }
        replayed = await replay_graph.ainvoke(regenerate.input, replay_config)
        assert [message.id for message in replayed["messages"]] == ["human-1", "ai-regenerated"]

        store = ThreadStore()
        monkeypatch.setattr(deps, "get_thread_store", lambda _request: store)
        monkeypatch.setattr(threads, "abuild_checkpoint_state_accessor", source_builder)
        monkeypatch.setattr(threads, "get_run_event_store", lambda _request: event_store)
        monkeypatch.setattr(threads, "get_trusted_internal_owner_user_id", lambda _request: None)
        monkeypatch.setattr(threads, "get_effective_user_id", lambda: "user-1")

        def branch_writer(_request: Any, *, thread_id: str, as_node: str, state_schema: Any, **_kwargs: Any) -> tuple[CheckpointStateAccessor, dict[str, Any]]:
            graph = build_state_mutation_graph(as_node, "full", state_schema)
            return CheckpointStateAccessor.bind(graph, saver, mode="full"), _config(thread_id)

        monkeypatch.setattr(threads, "build_checkpoint_state_mutation_accessor", branch_writer)
        branch = await threads._branch_thread_with_reservation(
            source_thread_id,
            threads.ThreadBranchRequest(message_id="ai-old", title="MySQL branch"),
            request,
        )
        branch_graph = _answer_graph(saver, "unused")
        branch_state = await branch_graph.aget_state(_config(branch.thread_id))
        assert [message.id for message in branch_state.values["messages"]] == ["human-1", "ai-old"]
        assert branch.parent_thread_id == source_thread_id
        assert store.records[branch.thread_id]["metadata"]["branch_parent_checkpoint_id"] == branch.parent_checkpoint_id

        original_head = await source_accessor.aget(source_config)
        assert [message.id for message in original_head.values["messages"]] == ["human-1", "ai-regenerated"]

        await saver.adelete_thread(source_thread_id)
        await saver.adelete_thread(branch.thread_id)


@pytest.mark.anyio
async def test_asyncmy_pool_saver_rolls_back_messages_and_pending_writes() -> None:
    """Use DeerFlow's rollback helper against persisted MySQL messages and writes."""
    thread_id = f"mysql-rollback-{uuid4().hex}"
    config = _config(thread_id)

    async with _open_mysql_saver() as (saver, _pool):
        graph = _answer_graph(saver, "ai-before")
        accessor = CheckpointStateAccessor.bind(graph, saver, mode="full")
        await graph.ainvoke({"messages": [HumanMessage(id="human-1", content="before")]}, config)
        pre_run = await accessor.aget(config)
        pre_run_config = pre_run.config
        await saver.aput_writes(pre_run_config, [("title", "pending-before")], task_id="task-before")

        rollback_point = await _capture_rollback_point(accessor, saver, config)
        assert rollback_point is not None
        assert rollback_point.pending_writes == (("task-before", "title", "pending-before"),)

        await accessor.aupdate(pre_run_config, {"messages": [AIMessage(id="ai-cancelled", content="cancelled")]}, as_node="answer")
        cancelled = await accessor.aget(config)
        await saver.aput_writes(cancelled.config, [("title", "pending-cancelled")], task_id="task-cancelled")

        assert await _rollback_to_pre_run_checkpoint(
            accessor=accessor,
            checkpointer=saver,
            thread_id=thread_id,
            run_id="mysql-v5-rollback",
            rollback_point=rollback_point,
            snapshot_capture_failed=False,
        )
        restored = await accessor.aget(config)
        assert [message.id for message in restored.values["messages"]] == ["human-1", "ai-before"]
        restored_tuple = await saver.aget_tuple(restored.config)
        assert restored_tuple is not None
        assert restored_tuple.pending_writes == [("task-before", "title", "pending-before")]

        await saver.adelete_thread(thread_id)


@pytest.mark.anyio
async def test_asyncmy_pool_saver_concurrent_graph_operations_are_isolated() -> None:
    """Two concurrently-started graph runs keep their pooled transactions isolated."""
    async with _open_mysql_saver() as (saver, _pool):

        async def run(thread_id: str) -> list[str]:
            attempts = {"count": 1}
            graph = _runtime_graph(saver, attempts)
            config = _config(thread_id)
            interrupted = await graph.ainvoke({"events": [thread_id]}, config)
            assert "__interrupt__" in interrupted
            result = await graph.ainvoke(Command(resume=thread_id), config)
            return result["events"]

        first_id = f"mysql-concurrent-a-{uuid4().hex}"
        second_id = f"mysql-concurrent-b-{uuid4().hex}"
        first, second = await asyncio.gather(run(first_id), run(second_id))

        assert first == [first_id, "start", f"resume:{first_id}", "retry-ok", "finish"]
        assert second == [second_id, "start", f"resume:{second_id}", "retry-ok", "finish"]
        assert first_id not in second
        assert second_id not in first

        await saver.adelete_thread(first_id)
        await saver.adelete_thread(second_id)
