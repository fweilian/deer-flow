### Generic Channel System (`app/channels/`)

This package contains the provider-neutral messaging runtime. Built-in adapters
are intentionally absent; trusted extensions may register custom `Channel`
implementations through `ChannelService` and `register_channel()`.

Keep these contracts stable:

- `base.py` defines the async `Channel` lifecycle and `send(OutboundMessage)`.
- `message_bus.py` routes `InboundMessage` into the manager and outbound
  messages back to the registered channel.
- `manager.py` owns thread mapping, commands, identity, attachment uploads, and
  LangGraph run dispatch. It must not import a concrete provider.
- `service.py` starts and stops the registered channels; an empty registry is a
  valid startup state.
- `store.py`, `dedupe_store.py`, `connection_identity.py`,
  `runtime_config_store.py`, and `app/gateway/routers/channel_connections.py`
  preserve user-owned connection state and identity mapping.
- `sandbox_files.py` and the inbound file reader provide the shared attachment
  path; adapters must not expose provider credentials in file URLs or messages.

Message flow is deliberately symmetric:

```text
custom transport -> InboundMessage -> MessageBus -> ChannelManager -> run
run result -> OutboundMessage -> MessageBus -> Channel.send()
```

The generic channel and connection routers remain available even when the
provider registry is empty. Database tables `channel_connections` and
`webhook_delivery` are retained for existing data and future extensions.
