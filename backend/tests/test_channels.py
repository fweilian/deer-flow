"""Contract tests for the generic Channel framework."""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.channels.base import Channel
from app.channels.manager import ChannelManager
from app.channels.message_bus import InboundMessage, InboundMessageType, MessageBus, OutboundMessage
from app.channels.run_policy import ChannelRunPolicy
from app.channels.service import ChannelService, get_channel_registrations, register_channel
from app.channels.store import ChannelStore


def _run(coro):
    return asyncio.run(coro)


class GenericChannel(Channel):
    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def send(self, msg: OutboundMessage) -> None:
        self.sent = msg


def test_inbound_message_flows_through_bus_and_outbound_reaches_channel():
    async def scenario():
        bus = MessageBus()
        channel = GenericChannel("generic", bus, {})
        received: list[InboundMessage] = []
        bus.subscribe_outbound(channel._on_outbound)

        inbound = InboundMessage("generic", "chat", "user", "hello")
        await bus.publish_inbound(inbound)
        assert await bus.get_inbound() == inbound

        outbound = OutboundMessage("generic", "chat", "thread", "reply")
        await bus.publish_outbound(outbound)
        assert channel.sent is outbound
        received.append(inbound)
        return received

    assert _run(scenario())


def test_channel_manager_uses_generic_non_streaming_contract(tmp_path: Path):
    async def scenario():
        bus = MessageBus()
        manager = ChannelManager(bus=bus, store=ChannelStore(tmp_path / "channels.json"))
        client = MagicMock()
        client.threads.create = AsyncMock(return_value={"thread_id": "thread-1"})
        client.runs.wait = AsyncMock(return_value={"messages": [{"type": "ai", "content": "reply"}]})
        manager._client = client
        await manager._handle_chat(InboundMessage("generic", "chat", "user", "hello"))
        client.runs.wait.assert_awaited_once()

    _run(scenario())


def test_channel_service_has_no_builtin_adapters():
    assert get_channel_registrations() == {}
    assert ChannelService({}).get_status() == {"service_running": False, "channels": {}}


def test_channel_registration_is_extension_friendly():
    register_channel("test-generic", "test_channels:GenericChannel", display_name="Generic")
    try:
        registration = get_channel_registrations()["test-generic"]
        assert registration.display_name == "Generic"
        assert registration.import_path.endswith("GenericChannel")
    finally:
        get_channel_registrations().pop("test-generic", None)
        # The registry is intentionally process-local; remove the test entry.
        from app.channels import service

        service._CHANNEL_REGISTRY.pop("test-generic", None)


def test_channel_service_starts_and_stops_registered_extension_channel(tmp_path: Path, monkeypatch):
    from app.channels import service as channel_service

    monkeypatch.setattr(channel_service, "ChannelStore", lambda: ChannelStore(tmp_path / "channels.json"))
    register_channel("test-generic", "test_channels:GenericChannel")
    try:

        async def scenario():
            service = ChannelService({"test-generic": {"enabled": True}})
            await service.start()
            assert service.get_channel("test-generic").is_running is True
            await service.stop()
            assert service.get_channel("test-generic") is None

        _run(scenario())
    finally:
        channel_service._CHANNEL_REGISTRY.pop("test-generic", None)


def test_default_channel_policy_is_interactive():
    policy = ChannelRunPolicy()
    assert policy.is_interactive is True
    assert policy.fire_and_forget is False


class TestMessageBus:
    def test_publish_and_get_inbound(self):
        bus = MessageBus()

        async def go():
            msg = InboundMessage(
                channel_name="test",
                chat_id="chat1",
                user_id="user1",
                text="hello",
            )
            await bus.publish_inbound(msg)
            result = await bus.get_inbound()
            assert result.text == "hello"
            assert result.channel_name == "test"
            assert result.chat_id == "chat1"

        _run(go())

    def test_inbound_queue_is_fifo(self):
        bus = MessageBus()

        async def go():
            for i in range(3):
                await bus.publish_inbound(InboundMessage(channel_name="test", chat_id="c", user_id="u", text=f"msg{i}"))
            for i in range(3):
                msg = await bus.get_inbound()
                assert msg.text == f"msg{i}"

        _run(go())

    def test_outbound_callback(self):
        bus = MessageBus()
        received = []

        async def callback(msg):
            received.append(msg)

        async def go():
            bus.subscribe_outbound(callback)
            out = OutboundMessage(channel_name="test", chat_id="c1", thread_id="t1", text="reply")
            await bus.publish_outbound(out)
            assert len(received) == 1
            assert received[0].text == "reply"

        _run(go())

    def test_unsubscribe_outbound(self):
        bus = MessageBus()
        received = []

        async def callback(msg):
            received.append(msg)

        async def go():
            bus.subscribe_outbound(callback)
            bus.unsubscribe_outbound(callback)
            out = OutboundMessage(channel_name="test", chat_id="c1", thread_id="t1", text="reply")
            await bus.publish_outbound(out)
            assert len(received) == 0

        _run(go())

    def test_unsubscribe_outbound_removes_fresh_bound_method_reference(self):
        bus = MessageBus()
        received = []

        class Handler:
            async def callback(self, msg):
                received.append((self, msg))

        handler = Handler()
        other_handler = Handler()

        async def go():
            bus.subscribe_outbound(handler.callback)
            bus.subscribe_outbound(other_handler.callback)
            bus.unsubscribe_outbound(handler.callback)
            out = OutboundMessage(channel_name="test", chat_id="c1", thread_id="t1", text="reply")
            await bus.publish_outbound(out)
            assert received == [(other_handler, out)]

        _run(go())

    def test_outbound_error_does_not_crash(self):
        bus = MessageBus()

        async def bad_callback(msg):
            raise ValueError("boom")

        received = []

        async def good_callback(msg):
            received.append(msg)

        async def go():
            bus.subscribe_outbound(bad_callback)
            bus.subscribe_outbound(good_callback)
            out = OutboundMessage(channel_name="test", chat_id="c1", thread_id="t1", text="reply")
            await bus.publish_outbound(out)
            assert len(received) == 1

        _run(go())

    def test_inbound_message_defaults(self):
        msg = InboundMessage(channel_name="test", chat_id="c", user_id="u", text="hi")
        assert msg.msg_type == InboundMessageType.CHAT
        assert msg.thread_ts is None
        assert msg.files == []
        assert msg.metadata == {}
        assert msg.created_at > 0

    def test_outbound_message_defaults(self):
        msg = OutboundMessage(channel_name="test", chat_id="c", thread_id="t", text="hi")
        assert msg.artifacts == []
        assert msg.is_final is True
        assert msg.thread_ts is None
        assert msg.metadata == {}


# ---------------------------------------------------------------------------
# ChannelStore tests
# ---------------------------------------------------------------------------


class TestChannelStore:
    @pytest.fixture
    def store(self, tmp_path):
        return ChannelStore(path=tmp_path / "store.json")

    def test_set_and_get_thread_id(self, store):
        store.set_thread_id("custom", "ch1", "thread-abc", user_id="u1")
        assert store.get_thread_id("custom", "ch1") == "thread-abc"

    def test_get_nonexistent_returns_none(self, store):
        assert store.get_thread_id("custom", "nonexistent") is None

    def test_remove(self, store):
        store.set_thread_id("custom", "ch1", "t1")
        assert store.remove("custom", "ch1") is True
        assert store.get_thread_id("custom", "ch1") is None

    def test_remove_nonexistent_returns_false(self, store):
        assert store.remove("custom", "nope") is False

    def test_list_entries_all(self, store):
        store.set_thread_id("custom", "ch1", "t1")
        store.set_thread_id("other-custom", "ch2", "t2")
        entries = store.list_entries()
        assert len(entries) == 2

    def test_list_entries_filtered(self, store):
        store.set_thread_id("custom", "ch1", "t1")
        store.set_thread_id("other-custom", "ch2", "t2")
        entries = store.list_entries(channel_name="custom")
        assert len(entries) == 1
        assert entries[0]["channel_name"] == "custom"

    def test_channel_store_concurrent_list_and_mutation(self, store, monkeypatch):
        iteration_started = threading.Event()
        mutation_requested = threading.Event()
        mutation_finished = threading.Event()

        class CoordinatedData(dict):
            def items(self):
                iterator = iter(super().items())
                first = next(iterator)
                iteration_started.set()

                if store._lock.locked():
                    assert mutation_requested.wait(timeout=5), "mutation thread never requested the store lock"
                else:
                    assert mutation_finished.wait(timeout=5), "mutation thread never changed the unlocked store"

                yield first
                yield from iterator

        store._data = CoordinatedData(
            {
                "custom:ch1": {"thread_id": "t1", "user_id": "u1", "created_at": 1.0, "updated_at": 1.0},
                "other-custom:ch2": {"thread_id": "t2", "user_id": "u2", "created_at": 2.0, "updated_at": 2.0},
            }
        )
        monkeypatch.setattr(store, "_save", lambda: None)

        def mutate():
            assert iteration_started.wait(timeout=5), "list_entries never started iterating"
            mutation_requested.set()
            store.set_thread_id("test", "new", "t3")
            mutation_finished.set()

        with ThreadPoolExecutor(max_workers=2) as executor:
            list_future = executor.submit(store.list_entries)
            mutation_future = executor.submit(mutate)
            mutation_future.result(timeout=5)
            entries = list_future.result(timeout=5)

        assert {(entry["channel_name"], entry["chat_id"]) for entry in entries} == {("custom", "ch1"), ("other-custom", "ch2")}

    def test_persistence(self, tmp_path):
        path = tmp_path / "store.json"
        store1 = ChannelStore(path=path)
        store1.set_thread_id("custom", "ch1", "t1")

        store2 = ChannelStore(path=path)
        assert store2.get_thread_id("custom", "ch1") == "t1"

    def test_update_preserves_created_at(self, store):
        store.set_thread_id("custom", "ch1", "t1")
        entries = store.list_entries()
        created_at = entries[0]["created_at"]

        store.set_thread_id("custom", "ch1", "t2")
        entries = store.list_entries()
        assert entries[0]["created_at"] == created_at
        assert entries[0]["thread_id"] == "t2"
        assert entries[0]["updated_at"] >= created_at

    def test_corrupt_file_handled(self, tmp_path):
        path = tmp_path / "store.json"
        path.write_text("not json", encoding="utf-8")
        store = ChannelStore(path=path)
        assert store.get_thread_id("x", "y") is None


# ---------------------------------------------------------------------------
# Channel base class tests
# ---------------------------------------------------------------------------


class DummyChannel(Channel):
    """Concrete test implementation of Channel."""

    def __init__(self, bus, config=None):
        super().__init__(name="dummy", bus=bus, config=config or {})
        self.sent_messages: list[OutboundMessage] = []
        self._running = False

    async def start(self):
        self._running = True
        self.bus.subscribe_outbound(self._on_outbound)

    async def stop(self):
        self._running = False
        self.bus.unsubscribe_outbound(self._on_outbound)

    async def send(self, msg: OutboundMessage):
        self.sent_messages.append(msg)


class TestChannelBase:
    def test_make_inbound(self):
        bus = MessageBus()
        ch = DummyChannel(bus)
        msg = ch._make_inbound(
            chat_id="c1",
            user_id="u1",
            text="hello",
            msg_type=InboundMessageType.COMMAND,
        )
        assert msg.channel_name == "dummy"
        assert msg.chat_id == "c1"
        assert msg.text == "hello"
        assert msg.msg_type == InboundMessageType.COMMAND

    def test_on_outbound_routes_to_channel(self):
        bus = MessageBus()
        ch = DummyChannel(bus)

        async def go():
            await ch.start()
            msg = OutboundMessage(channel_name="dummy", chat_id="c1", thread_id="t1", text="hi")
            await bus.publish_outbound(msg)
            assert len(ch.sent_messages) == 1

        _run(go())

    def test_on_outbound_ignores_other_channels(self):
        bus = MessageBus()
        ch = DummyChannel(bus)

        async def go():
            await ch.start()
            msg = OutboundMessage(channel_name="other", chat_id="c1", thread_id="t1", text="hi")
            await bus.publish_outbound(msg)
            assert len(ch.sent_messages) == 0

        _run(go())

    def test_send_with_retry_retries_until_success(self, monkeypatch):
        bus = MessageBus()
        ch = DummyChannel(bus)
        attempts = 0
        sleep = AsyncMock()
        monkeypatch.setattr("app.channels.base.asyncio.sleep", sleep)

        async def flaky_send():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise RuntimeError(f"failure {attempts}")
            return "sent"

        result = _run(ch._send_with_retry(flaky_send, max_retries=3, log_prefix="[Dummy]"))

        assert result == "sent"
        assert attempts == 3
        assert [call.args[0] for call in sleep.await_args_list] == [1, 2]

    def test_log_future_error_handles_cancelled_future(self, caplog):
        bus = MessageBus()
        ch = DummyChannel(bus)
        fut = Future()
        fut.cancel()

        with caplog.at_level(logging.ERROR):
            ch._log_future_error(fut, "prepare_inbound", "m1")

        assert "prepare_inbound" not in caplog.text

    def test_log_future_error_surfaces_future_exception(self, caplog):
        bus = MessageBus()
        ch = DummyChannel(bus)
        fut = Future()
        fut.set_exception(RuntimeError("boom"))

        with caplog.at_level(logging.ERROR):
            ch._log_future_error(fut, "prepare_inbound", "m1")

        assert "prepare_inbound failed for msg_id=m1: boom" in caplog.text


# ---------------------------------------------------------------------------
# _extract_response_text tests
# ---------------------------------------------------------------------------


class TestExtractResponseText:
    def test_string_content(self):
        from app.channels.manager import _extract_response_text

        result = {"messages": [{"type": "ai", "content": "hello"}]}
        assert _extract_response_text(result) == "hello"

    def test_list_content_blocks(self):
        from app.channels.manager import _extract_response_text

        result = {"messages": [{"type": "ai", "content": [{"type": "text", "text": "hello"}, {"type": "text", "text": " world"}]}]}
        assert _extract_response_text(result) == "hello world"

    def test_picks_last_ai_message(self):
        from app.channels.manager import _extract_response_text

        result = {
            "messages": [
                {"type": "ai", "content": "first"},
                {"type": "human", "content": "question"},
                {"type": "ai", "content": "second"},
            ]
        }
        assert _extract_response_text(result) == "second"

    def test_empty_messages(self):
        from app.channels.manager import _extract_response_text

        assert _extract_response_text({"messages": []}) == ""

    def test_no_ai_messages(self):
        from app.channels.manager import _extract_response_text

        result = {"messages": [{"type": "human", "content": "hi"}]}
        assert _extract_response_text(result) == ""

    def test_list_result(self):
        from app.channels.manager import _extract_response_text

        result = [{"type": "ai", "content": "from list"}]
        assert _extract_response_text(result) == "from list"

    def test_skips_empty_ai_content(self):
        from app.channels.manager import _extract_response_text

        result = {
            "messages": [
                {"type": "ai", "content": ""},
                {"type": "ai", "content": "actual response"},
            ]
        }
        assert _extract_response_text(result) == "actual response"

    def test_clarification_tool_message(self):
        from app.channels.manager import _extract_response_text

        result = {
            "messages": [
                {"type": "human", "content": "健身"},
                {"type": "ai", "content": "", "tool_calls": [{"name": "ask_clarification", "args": {"question": "您想了解哪方面？"}}]},
                {"type": "tool", "name": "ask_clarification", "content": "您想了解哪方面？"},
            ]
        }
        assert _extract_response_text(result) == "您想了解哪方面？"

    def test_clarification_over_empty_ai(self):
        """When AI content is empty but ask_clarification tool message exists, use the tool message."""
        from app.channels.manager import _extract_response_text

        result = {
            "messages": [
                {"type": "ai", "content": ""},
                {"type": "tool", "name": "ask_clarification", "content": "Could you clarify?"},
            ]
        }
        assert _extract_response_text(result) == "Could you clarify?"

    def test_does_not_leak_previous_turn_text(self):
        """When current turn AI has no text (only tool calls), do not return previous turn's text."""
        from app.channels.manager import _extract_response_text

        result = {
            "messages": [
                {"type": "human", "content": "hello"},
                {"type": "ai", "content": "Hi there!"},
                {"type": "human", "content": "export data"},
                {
                    "type": "ai",
                    "content": "",
                    "tool_calls": [{"name": "present_files", "args": {"filepaths": ["/mnt/user-data/outputs/data.csv"]}}],
                },
                {"type": "tool", "name": "present_files", "content": "ok"},
            ]
        }
        # Should return "" (no text in current turn), NOT "Hi there!" from previous turn
        assert _extract_response_text(result) == ""

    def test_ignores_hidden_human_control_messages(self):
        """Hidden control messages should not terminate current-turn response extraction."""
        from app.channels.manager import _extract_response_text

        result = {
            "messages": [
                {"type": "human", "content": "plan this"},
                {"type": "ai", "content": "Here is the plan."},
                {
                    "type": "human",
                    "name": "todo_reminder",
                    "content": "keep todos updated",
                    "additional_kwargs": {"hide_from_ui": True},
                },
            ]
        }

        assert _extract_response_text(result) == "Here is the plan."


class TestClarificationDetection:
    def test_final_clarification_tool_message_is_pending(self):
        from app.channels.manager import _has_current_turn_clarification

        result = {
            "messages": [
                {"type": "human", "content": "deploy"},
                {"type": "ai", "content": "", "tool_calls": [{"name": "ask_clarification", "args": {}}]},
                {"type": "tool", "name": "ask_clarification", "content": "Which environment?"},
            ]
        }
        assert _has_current_turn_clarification(result) is True

    def test_clarification_followed_by_regular_ai_is_not_pending(self):
        from app.channels.manager import _has_current_turn_clarification

        result = {
            "messages": [
                {"type": "human", "content": "deploy"},
                {"type": "ai", "content": "", "tool_calls": [{"name": "ask_clarification", "args": {}}]},
                {"type": "tool", "name": "ask_clarification", "content": "Which environment?"},
                {"type": "ai", "content": "I will continue without pending clarification."},
            ]
        }
        assert _has_current_turn_clarification(result) is False

    def test_previous_turn_clarification_does_not_mark_current_turn(self):
        from app.channels.manager import _has_current_turn_clarification

        result = {
            "messages": [
                {"type": "human", "content": "deploy"},
                {"type": "ai", "content": "", "tool_calls": [{"name": "ask_clarification", "args": {}}]},
                {"type": "tool", "name": "ask_clarification", "content": "Which environment?"},
                {"type": "human", "content": "prod"},
                {"type": "ai", "content": "Deploying to prod."},
            ]
        }
        assert _has_current_turn_clarification(result) is False


# ---------------------------------------------------------------------------
# ChannelManager tests
# ---------------------------------------------------------------------------


def _make_mock_langgraph_client(thread_id="test-thread-123", run_result=None):
    """Create a mock langgraph_sdk async client."""
    mock_client = MagicMock()

    # threads.create() returns a Thread-like dict
    mock_client.threads.create = AsyncMock(return_value={"thread_id": thread_id})
    mock_client.threads.update = AsyncMock(return_value={"thread_id": thread_id})

    # threads.get() returns thread info (succeeds by default)
    mock_client.threads.get = AsyncMock(return_value={"thread_id": thread_id})

    # runs.wait() returns the final state with messages
    if run_result is None:
        run_result = {
            "messages": [
                {"type": "human", "content": "hi"},
                {"type": "ai", "content": "Hello from agent!"},
            ]
        }
    mock_client.runs.wait = AsyncMock(return_value=run_result)

    return mock_client


async def _make_channel_connection_repo(tmp_path: Path):
    from deerflow.persistence.channel_connections import ChannelConnectionRepository, ChannelCredentialCipher
    from deerflow.persistence.engine import get_session_factory, init_engine

    await init_engine("sqlite", url=f"sqlite+aiosqlite:///{tmp_path / 'channel-connections.db'}", sqlite_dir=str(tmp_path))
    return ChannelConnectionRepository(
        get_session_factory(),
        cipher=ChannelCredentialCipher.from_key("test-channel-key"),
    )


def _make_stream_part(event: str, data):
    return SimpleNamespace(event=event, data=data)


def _ok_stream_events():
    """Minimal successful streaming run: one text chunk plus a final values frame."""
    return [
        _make_stream_part(
            "messages-tuple",
            [{"id": "ai-1", "content": "Hello", "type": "AIMessageChunk"}, {"langgraph_node": "agent"}],
        ),
        _make_stream_part(
            "values",
            {"messages": [{"type": "human", "content": "hi"}, {"type": "ai", "content": "Hello"}], "artifacts": []},
        ),
    ]


def _make_async_iterator(items):
    async def iterator():
        for item in items:
            yield item

    return iterator()
