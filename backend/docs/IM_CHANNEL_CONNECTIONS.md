# Generic Channel Connections

DeerFlow keeps a provider-neutral connection layer for custom Channel
extensions. Built-in provider adapters are not included in this phase, but the
connection APIs and persistence remain available for future adapters.

## Contracts

- `InboundMessage` is the normalized upstream message contract.
- `OutboundMessage` is the normalized downstream delivery contract.
- `MessageBus` publishes inbound messages to `ChannelManager` and routes
  outbound messages to the owning `Channel.send()` implementation.
- `ChannelManager` owns thread mapping, identity mapping, commands, run policy,
  and shared attachment uploads.
- `ChannelService` starts registered adapters and accepts an empty registry.
- `Channel` implementations are registered by trusted extensions; registration
  supplies the import path, display name, authentication mode, credential
  fields, and optional runtime requirements.

```text
transport event
    -> InboundMessage
    -> MessageBus.publish_inbound()
    -> ChannelManager
    -> LangGraph run
    -> OutboundMessage
    -> MessageBus.publish_outbound()
    -> Channel.send()
```

## Configuration

The generic shape in `config.yaml` is intentionally empty by default:

```yaml
channel_connections:
  enabled: false
  require_bound_identity: true
  providers: {}
```

An extension can add its provider configuration under `providers` and register
the matching Channel during trusted Gateway startup. The generic router does
not assume a fixed provider list and reports an empty provider map when none is
registered.

## Connection APIs

The following routes remain supported for registered providers:

- `GET /api/channels` and `GET /api/channels/{provider}` expose registration
  and runtime status.
- `POST /api/channels/{provider}/connect` creates a short-lived bind code.
- `POST /api/channels/{provider}/runtime-config` updates extension-owned
  runtime settings.
- `DELETE /api/channels/{provider}/runtime-config` removes those settings.
- `GET /api/channel-connections` lists the current user's connections.
- `POST /api/channel-connections/{provider}/disconnect` revokes a connection.

Adapters should use the existing `/connect <code>` command (or document an
equivalent transport-native flow), call `ChannelManager.handle_inbound()` with
an `InboundMessage`, and return outbound text or files only through
`OutboundMessage`. Identity binding is explicit and owner-scoped; provider
identifiers are normalized before they reach filesystem paths.

## Persistence and compatibility

The SQL tables `channel_connections` and `webhook_delivery`, their migrations,
and the channel connection repository are retained. This change does not drop,
rewrite, or migrate historical rows. New adapters may reuse the existing
connection, conversation, dedupe, and delivery records.

Attachments must use `sandbox_files.py` and the shared upload path. Do not put
provider tokens in message content, artifact URLs, persisted thread metadata,
or client-visible responses.
