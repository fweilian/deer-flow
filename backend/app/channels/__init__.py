"""IM Channel integration for DeerFlow.

Provides a pluggable channel system for connecting external messaging platforms
to the DeerFlow agent via the ChannelManager and MessageBus.
"""

from app.channels.base import Channel
from app.channels.message_bus import InboundMessage, MessageBus, OutboundMessage

__all__ = [
    "Channel",
    "InboundMessage",
    "MessageBus",
    "OutboundMessage",
]
