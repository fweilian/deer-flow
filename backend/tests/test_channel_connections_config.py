"""Tests for generic user-owned Channel connection configuration."""

from deerflow.config.channel_connections_config import ChannelConnectionsConfig


def test_channel_connections_disabled_by_default():
    config = ChannelConnectionsConfig()

    assert config.enabled is False
    assert config.require_bound_identity is True
    assert config.providers == {}
    assert config.provider_status("custom") == {"enabled": False, "configured": False}


def test_generic_provider_configuration_is_preserved():
    config = ChannelConnectionsConfig.model_validate(
        {
            "enabled": True,
            "providers": {"custom": {"enabled": True, "settings": {"endpoint": "https://example.test"}}},
        }
    )

    assert config.enabled is True
    assert config.providers["custom"].settings == {"endpoint": "https://example.test"}
    assert config.provider_status("custom") == {"enabled": True, "configured": True}


def test_require_bound_identity_can_be_disabled():
    config = ChannelConnectionsConfig.model_validate({"enabled": True, "require_bound_identity": False})

    assert config.require_bound_identity is False
