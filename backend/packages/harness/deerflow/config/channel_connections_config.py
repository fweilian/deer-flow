"""Configuration for user-owned connections to registered Channels."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ChannelConnectionProviderConfig(BaseModel):
    """Operator settings for one extension-provided Channel."""

    enabled: bool = False
    settings: dict[str, Any] = Field(default_factory=dict)


class ChannelConnectionsConfig(BaseModel):
    """Top-level config for browser-connectable, extension-provided Channels."""

    enabled: bool = False
    require_bound_identity: bool = True
    providers: dict[str, ChannelConnectionProviderConfig] = Field(default_factory=dict)
    model_config = ConfigDict(extra="ignore")

    def provider_status(self, provider: str) -> dict[str, bool]:
        config = self.providers.get(provider)
        if config is None:
            return {"enabled": False, "configured": False}
        return {"enabled": bool(config.enabled), "configured": bool(config.enabled)}
