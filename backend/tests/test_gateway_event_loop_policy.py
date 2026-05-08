from importlib import import_module
from unittest.mock import MagicMock

gateway_app = import_module("app.gateway.app")


def test_configure_windows_event_loop_policy_sets_selector_on_windows(monkeypatch):
    fake_policy_cls = type("FakeWindowsSelectorEventLoopPolicy", (), {})
    old_policy = object()
    set_policy = MagicMock()

    monkeypatch.setattr(gateway_app.sys, "platform", "win32")
    monkeypatch.setattr(gateway_app.asyncio, "WindowsSelectorEventLoopPolicy", fake_policy_cls, raising=False)
    monkeypatch.setattr(gateway_app.asyncio, "get_event_loop_policy", lambda: old_policy)
    monkeypatch.setattr(gateway_app.asyncio, "set_event_loop_policy", set_policy)

    gateway_app._configure_windows_event_loop_policy()

    set_policy.assert_called_once()
    assert isinstance(set_policy.call_args.args[0], fake_policy_cls)


def test_configure_windows_event_loop_policy_is_noop_off_windows(monkeypatch):
    set_policy = MagicMock()

    monkeypatch.setattr(gateway_app.sys, "platform", "linux")
    monkeypatch.setattr(gateway_app.asyncio, "set_event_loop_policy", set_policy)

    gateway_app._configure_windows_event_loop_policy()

    set_policy.assert_not_called()
