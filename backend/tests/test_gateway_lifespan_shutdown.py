"""Regression tests for Gateway lifespan shutdown.

These tests guard the invariant that lifespan shutdown is *bounded*: a
misbehaving channel whose ``stop()`` blocks forever must not keep the
uvicorn worker alive. A hung worker is the precondition for the
signal-reentrancy deadlock described in
``app.gateway.app._SHUTDOWN_HOOK_TIMEOUT_SECONDS``.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from pytest import LogCaptureFixture


@asynccontextmanager
async def _noop_langgraph_runtime(_app):
    yield


async def _run_lifespan_with_hanging_stop() -> float:
    """Drive the lifespan context with stop_channel_service hanging forever.

    Returns the elapsed wall-clock seconds.
    """
    from app.gateway.app import _SHUTDOWN_HOOK_TIMEOUT_SECONDS, lifespan

    async def hang_forever() -> None:
        await asyncio.sleep(3600)

    app = FastAPI()

    fake_service = MagicMock()
    fake_service.get_status = MagicMock(return_value={})

    async def fake_start():
        return fake_service

    with (
        patch("app.gateway.app.get_app_config"),
        patch("app.gateway.app.get_gateway_config", return_value=MagicMock(host="x", port=0)),
        patch("app.gateway.app.langgraph_runtime", _noop_langgraph_runtime),
        patch("app.channels.service.start_channel_service", side_effect=fake_start),
        patch("app.channels.service.stop_channel_service", side_effect=hang_forever),
    ):
        loop = asyncio.get_event_loop()
        start = loop.time()
        async with lifespan(app):
            pass
        elapsed = loop.time() - start

    assert _SHUTDOWN_HOOK_TIMEOUT_SECONDS < 30.0, "Timeout constant must stay modest"
    return elapsed


def test_shutdown_is_bounded_when_channel_stop_hangs():
    """Lifespan exit must complete near the configured timeout, not hang."""
    from app.gateway.app import _SHUTDOWN_HOOK_TIMEOUT_SECONDS

    elapsed = asyncio.run(_run_lifespan_with_hanging_stop())

    # Generous upper bound: timeout + 2s slack for scheduling overhead.
    assert elapsed < _SHUTDOWN_HOOK_TIMEOUT_SECONDS + 2.0, f"Lifespan shutdown took {elapsed:.2f}s; expected <= {_SHUTDOWN_HOOK_TIMEOUT_SECONDS + 2.0:.1f}s"
    # Lower bound: the wait_for should actually have waited.
    assert elapsed >= _SHUTDOWN_HOOK_TIMEOUT_SECONDS - 0.5, f"Lifespan exited too quickly ({elapsed:.2f}s); wait_for may not have been invoked."


async def _run_lifespan_with_cron_hooks() -> None:
    from app.gateway.app import lifespan

    app = FastAPI()

    fake_service = MagicMock()
    fake_service.get_status = MagicMock(return_value={})

    async def fake_start_channel():
        return fake_service

    with (
        patch("app.gateway.app.get_app_config"),
        patch("app.gateway.app.get_gateway_config", return_value=MagicMock(host="x", port=0)),
        patch("app.gateway.app.langgraph_runtime", _noop_langgraph_runtime),
        patch("app.channels.service.start_channel_service", side_effect=fake_start_channel),
        patch("app.channels.service.stop_channel_service", new=AsyncMock()),
        patch("app.gateway.app.start_gateway_cron_scheduler", new=AsyncMock()) as start_cron,
        patch("app.gateway.app.stop_gateway_cron_scheduler", new=AsyncMock()) as stop_cron,
    ):
        async with lifespan(app):
            pass

    start_cron.assert_awaited_once_with(app)
    stop_cron.assert_awaited_once_with(app)


def test_lifespan_starts_and_stops_cron_scheduler():
    asyncio.run(_run_lifespan_with_cron_hooks())


async def _run_stop_gateway_cron_scheduler_clears_state() -> None:
    from app.gateway.cron_scheduler import stop_gateway_cron_scheduler

    app = FastAPI()
    task = asyncio.create_task(asyncio.sleep(3600))
    app.state.cron_scheduler_task = task
    app.state.cron_scheduler_service = object()
    app.state.cron_scheduler_repo = object()

    await stop_gateway_cron_scheduler(app)

    assert app.state.cron_scheduler_task is None
    assert app.state.cron_scheduler_service is None
    assert app.state.cron_scheduler_repo is None


def test_stop_gateway_cron_scheduler_clears_scheduler_state():
    asyncio.run(_run_stop_gateway_cron_scheduler_clears_state())


async def _run_start_gateway_cron_scheduler_logs_started(caplog: LogCaptureFixture) -> None:
    from app.gateway.cron_scheduler import start_gateway_cron_scheduler

    app = FastAPI()
    app.state.stream_bridge = object()
    app.state.run_manager = object()
    app.state.checkpointer = object()
    app.state.run_event_store = object()
    app.state.thread_store = object()
    app.state.config = SimpleNamespace(run_events=None)

    fake_bus = SimpleNamespace(publish_outbound=AsyncMock())
    fake_channel_service = SimpleNamespace(bus=fake_bus)
    fake_session_factory = object()
    created_tasks: list[object] = []

    def _capture_task(coro):
        created_tasks.append(coro)
        coro.close()
        return SimpleNamespace(cancel=lambda: None)

    with (
        patch("app.gateway.cron_scheduler.get_session_factory", return_value=fake_session_factory),
        patch("app.gateway.cron_scheduler.CronSchedulerRepository") as repo_cls,
        patch("app.gateway.cron_scheduler.asyncio.create_task", side_effect=_capture_task) as create_task,
        patch("app.channels.service.get_channel_service", return_value=fake_channel_service),
        caplog.at_level(logging.INFO, logger="app.gateway.cron_scheduler"),
    ):
        repo_cls.return_value = SimpleNamespace()
        await start_gateway_cron_scheduler(app)

    create_task.assert_called_once()
    assert len(created_tasks) == 1
    assert any("Gateway cron scheduler started:" in record.message and "instance_id=gateway-" in record.message and "poll_interval=10.0s" in record.message and "lease_seconds=30s" in record.message for record in caplog.records)


def test_start_gateway_cron_scheduler_logs_started(caplog: LogCaptureFixture):
    asyncio.run(_run_start_gateway_cron_scheduler_logs_started(caplog))
