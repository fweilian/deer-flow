"""Blocking-I/O regression anchors for the generic runtime config store."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest import mock

import pytest

from app.channels.runtime_config_store import ChannelRuntimeConfigStore

pytestmark = pytest.mark.asyncio


async def test_runtime_config_store_file_io_is_offloaded(tmp_path) -> None:
    path = tmp_path / "channels" / "runtime-config.json"
    store = await asyncio.to_thread(ChannelRuntimeConfigStore, path)
    await asyncio.to_thread(store.set_provider_config, "custom", {"enabled": True, "token": "secret"})

    assert await asyncio.to_thread(store.get_provider_config, "custom") == {
        "enabled": True,
        "token": "secret",
    }
    assert await asyncio.to_thread(lambda: path.stat().st_mode & 0o777) == 0o600


async def test_runtime_config_store_overwrites_loose_existing_file(tmp_path) -> None:
    path = tmp_path / "channels" / "runtime-config.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o644)
    store = await asyncio.to_thread(ChannelRuntimeConfigStore, path)
    await asyncio.to_thread(store.set_provider_config, "custom", {"enabled": True})

    assert await asyncio.to_thread(lambda: path.stat().st_mode & 0o777) == 0o600


async def test_runtime_config_store_chmod_failure_is_logged_not_fatal(tmp_path, caplog) -> None:
    path = tmp_path / "channels" / "runtime-config.json"
    store = await asyncio.to_thread(ChannelRuntimeConfigStore, path)
    real_chmod = Path.chmod

    def chmod_spy(self: Path, mode: int, *args, **kwargs):
        if self.suffix == ".tmp":
            raise OSError("chmod unsupported on this filesystem")
        return real_chmod(self, mode, *args, **kwargs)

    def save() -> None:
        with caplog.at_level(logging.DEBUG, logger="app.channels.runtime_config_store"), mock.patch.object(Path, "chmod", chmod_spy):
            store.set_provider_config("custom", {"enabled": True})

    await asyncio.to_thread(save)
    assert any("Unable to chmod temporary channel runtime config store" in record.getMessage() for record in caplog.records)
