"""Precedence tests for generic Channel runtime configuration."""

from types import SimpleNamespace

from app.channels.runtime_config_store import merge_runtime_channel_configs
from deerflow.config.channel_connections_config import ChannelConnectionsConfig


def _store(data):
    return SimpleNamespace(load_all=lambda: data)


def test_runtime_config_wins_over_yaml_on_shared_keys():
    channels_config = {"custom": {"endpoint": "https://yaml.example", "mode": "chat"}}
    connections = ChannelConnectionsConfig.model_validate({"enabled": True, "providers": {"custom": {"enabled": True}}})
    store = _store({"custom": {"endpoint": "https://runtime.example", "token": "secret"}})

    merge_runtime_channel_configs(channels_config, connections, store=store)

    assert channels_config["custom"] == {
        "endpoint": "https://runtime.example",
        "mode": "chat",
        "token": "secret",
    }


def test_runtime_config_for_disabled_provider_is_ignored():
    channels_config: dict = {}
    connections = ChannelConnectionsConfig.model_validate({"enabled": True, "providers": {"custom": {"enabled": False}}})

    merge_runtime_channel_configs(channels_config, connections, store=_store({"custom": {"enabled": True}}))

    assert channels_config == {}
