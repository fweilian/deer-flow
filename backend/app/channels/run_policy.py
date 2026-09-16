"""Optional run-policy hooks for extension-provided Channels."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.channels.message_bus import InboundMessage


@dataclass(frozen=True, slots=True)
class ChannelRunPolicy:
    """Run behavior an extension may opt into for a registered Channel."""

    is_interactive: bool = True
    default_recursion_limit: int | None = None
    credentials_provider: Callable[[InboundMessage, dict[str, Any]], Awaitable[None]] | None = None
    requires_bound_identity: bool = True
    fire_and_forget: bool = False
    serialize_thread_runs: bool = False
    buffer_followups_on_busy: bool = False


# Providers absent from this map use the normal interactive-chat defaults.
CHANNEL_RUN_POLICY: dict[str, ChannelRunPolicy] = {}
