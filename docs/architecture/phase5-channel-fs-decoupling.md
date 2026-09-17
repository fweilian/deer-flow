# Channel Attachment Boundary — Deferred Design Note

**Status:** deferred; no Channel Attachment Transport or Channel/Sandbox redesign is selected for Phase 5.
**Baseline:** `cfd41bf6` (`chore: remove browser automation`).
**Related plan:** [`phase5-filesystem-rescan.md`](phase5-filesystem-rescan.md).

## 1. Confirmed boundary

`backend/app/channels/` is a permanent extension surface. The built-in adapters were removed, but the generic Channel framework, service, registry, routers, and connection-related persistence remain available for trusted extensions.

Phase 5 does not redesign future Channel attachment handling. In particular, it does not choose or implement:

- a Gateway internal HTTP endpoint;
- an attachment upload/download protocol;
- a new `ResolvedAttachment` model or `actual_path` compatibility contract;
- Channel-side or Gateway-side Sandbox synchronization/materialization;
- a Channel deployment topology.

The Phase 5 Object Storage work may establish the shared outputs/uploads namespaces, but Channel attachment integration is a later caller-design task. Do not add Channel-specific transport requirements to the Phase 5 implementation checklist.

## 2. Current filesystem call sites to retain as evidence

The following calls are known future integration points and are intentionally not changed by the Phase 5 core storage task:

| Location | Current dependency | Later concern |
|---|---|---|
| `app/channels/manager.py:732` | `resolve_outputs_confined_path` and local artifact inspection | Define an outputs-only attachment read contract |
| `app/channels/manager.py:797-859` | local uploads directory and `write_upload_file_no_symlink` | Define inbound attachment ingestion and naming semantics |
| `app/channels/sandbox_files.py` | Sandbox file synchronization helper | Decide whether/where Runtime or Sandbox materializes stored objects |
| `app/channels/store.py` | `channels/store.json` | Later DB-backed Channel connection persistence |
| `app/channels/runtime_config_store.py` | `channels/runtime-config.json` credentials | Later Credential Broker / secret-persistence decision |

These call sites do not change the Phase 5 completion condition: the Phase 5 source-of-truth requirement covers outputs/artifacts, uploads, `.tool-results`, Custom Skills, and Skill enable state—not Channel attachment transport.

## 3. Re-entry criteria

Reopen this design when a custom Channel requires attachment support. The follow-up design must then define, with its own behavior and security tests:

- inbound upload and outbound artifact-read transport;
- owner identity and authorization;
- filename collision and overwrite behavior;
- streaming, size limits, MIME, checksum, and failure isolation;
- how Runtime/Sandbox materializes data when a real local path is required;
- compatibility requirements for third-party Channel extensions.

Until that work is explicitly started, do not implement or pre-commit to any of those protocol choices.

## 4. Explicitly out of Phase 5

- Gateway internal Channel attachment endpoints;
- `ChannelAttachmentClient` or equivalent;
- `ResolvedAttachment` field changes;
- Channel-specific CSRF/client-token protocol;
- `sandbox_files.py` deletion or replacement;
- eager or on-demand Channel attachment Sandbox synchronization;
- Channel connection/config JSON migration to Object Storage.
