from __future__ import annotations

import time
from types import SimpleNamespace

from deerflow.community.cmb_sandbox.cmb_sandbox_provider import CmbSandboxProvider


class _FakeSandbox:
    created: list[_FakeSandbox] = []

    def __init__(self, id: str, thread_id: str):
        self.id = id
        self.thread_id = thread_id
        self.closed = False
        self.__class__.created.append(self)

    def close(self):
        self.closed = True


def test_acquire_reuses_thread_bound_sandbox(monkeypatch):
    _FakeSandbox.created = []
    monkeypatch.setattr(
        "deerflow.community.cmb_sandbox.cmb_sandbox_provider.get_app_config",
        lambda: SimpleNamespace(sandbox=SimpleNamespace(cmb_idle_timeout=0)),
    )
    monkeypatch.setattr(
        "deerflow.community.cmb_sandbox.cmb_sandbox_provider.CmbSandbox",
        _FakeSandbox,
    )

    provider = CmbSandboxProvider()

    sandbox_id_1 = provider.acquire("thread-a")
    sandbox_id_2 = provider.acquire("thread-a")

    assert sandbox_id_1 == sandbox_id_2
    assert len(_FakeSandbox.created) == 1
    assert provider.get(sandbox_id_1) is _FakeSandbox.created[0]

    provider.shutdown()
    assert _FakeSandbox.created[0].closed is True


def test_cleanup_idle_sandboxes_closes_stale_instances(monkeypatch):
    _FakeSandbox.created = []
    monkeypatch.setattr(
        "deerflow.community.cmb_sandbox.cmb_sandbox_provider.get_app_config",
        lambda: SimpleNamespace(sandbox=SimpleNamespace(cmb_idle_timeout=1)),
    )
    monkeypatch.setattr(
        "deerflow.community.cmb_sandbox.cmb_sandbox_provider.CmbSandbox",
        _FakeSandbox,
    )
    monkeypatch.setattr(
        CmbSandboxProvider,
        "_start_idle_checker",
        lambda self: None,
    )

    provider = CmbSandboxProvider()
    sandbox_id = provider.acquire("thread-b")
    provider._last_activity[sandbox_id] = time.time() - 30

    provider._cleanup_idle_sandboxes()

    assert provider.get(sandbox_id) is None
    assert _FakeSandbox.created[0].closed is True
    provider.shutdown()
