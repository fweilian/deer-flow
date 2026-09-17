# Phase 5 Storage / Filesystem Scope Re-scan

**Scope:** read-only re-scan of local filesystem dependencies at HEAD `cfd41bf6` (`chore: remove browser automation`).
**Method:** static search + call-chain inspection + config inspection. No code, config, or test was modified.
**Status:** investigation only. Phase 5 implementation not started.

> Old `docs/architecture/feature-inventory.md` is used only as a search lead. Every conclusion below is re-derived from current HEAD.

---

## 0. Premise corrections found during the re-scan

These must be read before the tables, because they define the Phase 5 boundary.

**P1 — Channel framework is a permanent extension surface; it is not a Phase 3 blocker.**
Phase 3 commit `9e9f1888` removed the built-in adapters (`feishu`/`slack`/`telegram`/`dingtalk`/`wechat`/`wecom`/`discord`/`buzz`/`github`), the `gateway/github/*` package, `routers/github_webhooks.py`, and `routers/integrations.py`. It intentionally retained `app/channels/` so trusted extensions can register custom Channel implementations. At HEAD `backend/app/channels/` still holds 14 modules / 5,372 LOC, is still started from the Gateway lifespan (`app/gateway/app.py:332-337` → `start_channel_service`), and still mounts two generic routers (`app/gateway/app.py:788` `/api/channels`, `app/gateway/app.py:791` `/api/channel-connections`).

This retention is intentional and documented — `backend/app/channels/AGENTS.md:3-5` and `:29-31`. The framework's primary future responsibilities are upstream and downstream messaging. Its current attachment filesystem calls are recorded as a future integration concern, but Phase 5 does **not** design a Channel Attachment Transport or change `ResolvedAttachment` / Channel–Sandbox interaction. `channels/store.json` and `channels/runtime-config.json` are connection/config persistence, not Phase 5 object-storage data types; assign them to later DB / Credential Broker work.

**P2 — Phase 1 already moved the DB / event / agent-definition layers off local disk.**
`config.yaml` at HEAD: `database.backend: postgres` (L210), `run_events.backend: db` (L221), `agent_storage.backend: db` (L225), `stream_bridge.type: redis` (L250). The old inventory's §1.3 risk R2 ("默认部署不是无状态的") is therefore **materially outdated**: the DB/event/agent-definition layers, projects, user preferences and scheduled tasks are already configured as shared state. This does not make checkpoint storage a Phase 5 task; checkpoint sizing and persistence remain a separate post-Phase-5 evaluation. The remaining local-disk surface is much smaller than the old inventory assumed.

**P3 — `.tool-results` is no longer at the thread root.**
Old inventory §8.2 placed it at `{thread}/.tool-results/`. At HEAD it is written **under `outputs/`**: `agents/middlewares/tool_output_budget_middleware.py:160-189` joins `outputs_path` (from `ThreadDataState["outputs_path"]`, `thread_data_middleware.py:65`) with `config.storage_subdir`; `tool_output_config.py` documents it as "directory name **under the thread outputs path**". Old §8.2 tree is stale on this point.

**P4 — the frontend workspace-changes UI was deleted in Phase 2, but the backend module is still live.**
Phase 2 (`432e2c09`) deleted `frontend/src/core/workspace-changes/*`, the workspace-change components, and `backend/tests/test_workspace_changes.py`. `backend/packages/harness/deerflow/workspace_changes/` **remains** and is still called from `runtime/runs/worker.py:91-92, 1478-1507` — it feeds produced-output detection and the run delivery receipt. It is not dead code.

**P5 — Custom Skills are Phase 5 runtime-persistent objects.**
User-authored Skills and `_skill_states.json` must be consistent across instances. Reuse the existing `SkillStorage` ABC and reflection factory (`skills/storage/skill_storage.py`, `skills/storage/__init__.py`); add a non-local backend. Bundled/Public Skills remain static deployment resources, and `skills_view/` remains a rebuildable Sandbox projection.

**P6 — `USER.md` is deleted; it is not migrated.**
The rescan found no Agent injection consumer, frontend consumer, or documentation consumer. The only live access is the `/api/agents/user-profile` GET/PUT endpoint itself. The confirmed Phase 5 boundary is to remove `USER.md` and those endpoints, together with their obsolete test/route surface.

**P7 — JWT secret and checkpoints are outside the Object Storage migration.**
Production must explicitly provide `AUTH_JWT_SECRET` so all instances share one signing secret. `.jwt_secret` remains only as the single-node/local-development fallback and is not a production persistence mechanism. Checkpoint storage migration is not part of Phase 5; after object-storage work, evaluate the actual checkpoint size distribution, write frequency, per-thread count, and large-payload cases before choosing a final persistence design. No checkpoint database migration is proposed here.

---

## 1. Remaining Filesystem Dependencies

Production rows only. Rows marked **(non-production)** are test/script/dev paths, listed for completeness and excluded from Phase 5 scope.

| # | Capability / Data | Code Location | Current Storage | Runtime / Static | Persistence Needed | Cross-instance Needed | Classification | Evidence | Risk |
|---|---|---|---|---|---|---|---|---|---|
| 1 | **Artifacts (agent outputs)** | `app/gateway/routers/artifacts.py` (`reserve_artifact_write` L63, `_replace_artifact_atomically` L123, `_sync_artifact_to_sandbox` L159, `get_artifact` L371, `update_artifact` L518); `app/gateway/path_utils.py::resolve_outputs_confined_path` L58; `app/gateway/artifact_archive.py::build_artifact_archive` L207 | `{DEER_FLOW_HOME}/users/{uid}/threads/{tid}/user-data/outputs/` | Runtime | Yes | Yes | **MOVE_TO_OBJECT_STORE** | `frontend/src/core/artifacts/utils.ts:71,73,83,96` calls `/api/threads/{tid}/artifacts…` and `/runs/{rid}/artifacts/archive`; `tools/builtins/present_file_tool.py:59-76` requires `thread_data["outputs_path"]` | **High** — `FileResponse` streaming, HTTP range slicing (`_slice_byte_range` L175), atomic temp+`os.replace` with `fchown`/`fchmod` (L123-156), mtime/size-keyed SHA-256 cache (L356), zip archive build, sandbox push (`_sync_artifact_to_sandbox`) all assume a real local file |
| 2 | **Uploads** | `app/gateway/routers/uploads.py` (staging `tempfile.mkstemp` L183, `get_paths().sandbox_uploads_dir` L237/241/248, `sandbox.update_file` L233); `harness/deerflow/uploads/manager.py` (`get_uploads_dir` L35, `write_upload_file_no_symlink` L279, `delete_file_safe` L321, `cleanup_stale_upload_staging_files` L165) | `{…}/user-data/uploads/` + `.upload-*.part` staging | Runtime | Yes | Yes | **MOVE_TO_OBJECT_STORE** | `frontend/src/core/uploads/api.ts:137,175,196` (`/api/threads/{tid}/uploads`, `/list`, `/{filename}`); `tools/builtins/list_uploaded_files_tool.py:129,165`; `agents/middlewares/uploads_middleware.py:229,247-249` verifies existence on disk before injecting the prompt block | **High** — streaming multipart write, staging-file lifecycle + stale cleanup, symlink guards, provider-dependent sandbox sync |
| 3 | **Large tool output `.tool-results`** | `agents/middlewares/tool_output_budget_middleware.py` (`_externalize` L160, `_externalize_to_sandbox` L192, `_resolve_outputs_path` L310); `config/tool_output_config.py` | `{…}/user-data/outputs/.tool-results/` **(under outputs — see P3)** | Runtime | Yes | Yes | **MOVE_TO_OBJECT_STORE** | `artifact_archive.py:243` reserves the dir name so it is excluded from archives; `workspace_changes/scanner.py` excludes it from produced-artifact detection; the model reads the returned `/mnt/user-data/outputs/.tool-results/…` path back via `read_file` in later turns | **Medium** — old inventory §8.2 location is stale. Externalization is retained; do not resolve this row by truncation or by placing the payload in Agent state/checkpoints |
| 4 | **Agent workspace** | `config/paths.py::sandbox_work_dir` L330; `sandbox/local/local_sandbox_provider.py:167`; `workspace_changes/recorder.py::build_thread_workspace_roots` L27-40; `mcp/tools.py::_prepare_stdio_workspace` L200-217; `community/aio_sandbox/aio_sandbox_provider.py:983` | `{…}/user-data/workspace/` | Runtime | **Yes — thread-scoped, not per-run** | Yes | **DEFER_TO_SANDBOX** | created by `ensure_thread_dirs` (`paths.py:415-435`), removed by `delete_thread_dir` (`paths.py:437-444`) → lifetime = thread, not run; scanned pre/post run by `workspace_changes`; bind-mounted into remote sandbox at `aio_sandbox_provider.py:983` | **Medium** — intentionally outside the narrowed Phase 5 completion condition |
| 5 | **ACP workspace** | `config/paths.py::acp_workspace_dir` L354; `tools/builtins/invoke_acp_agent_tool.py:42,45,47`; `agents/lead_agent/prompt.py:1004` | `{…}/acp-workspace/` | Runtime | Yes — thread-scoped | Yes | **DEFER_TO_SANDBOX** | same lifetime rule as row 4 (`ensure_thread_dirs` includes it, `paths.py:432`) | Low |
| 6 | **Thread persistent-object lifecycle (delete + branch copy)** | `app/gateway/routers/threads.py::_copy_branch_user_data_sync` L326-333 (`shutil.copytree`), `_ignore_branch_user_data` L314-323, `_delete_thread_data` L616-618 → `config/paths.py::delete_thread_dir` L437-444 | rows 1–3; Custom Skill lifecycle in row 8 follows its user-scoped storage contract; workspace rows 4–5 remain local/deferred | Runtime | Yes | Yes | **MOVE_TO_OBJECT_STORE** — an *operation* over Phase 5 objects, not a new data type | branch API surfaces `workspace_clone_mode` (`threads.py:602, 1091, 1099`); `_delete_thread_data` is the thread-delete path | **High** — `copytree`/`rmtree` semantics must be re-expressed as object-store copy/delete for thread-scoped objects; do not include Sandbox workspace lifecycle in this Phase 5 operation |
| 7 | **Memory (deermem file backend)** | `agents/memory/manager.py:944-955` injects `storage_path = runtime_home()`; `backends/deermem/deermem/core/storage.py` (`FileMemoryStorage` L477, `create_storage` L1830-1853); `core/paths.py`; `core/retrieval.py::FTS5Retrieval` L90 | `{base_dir}/users/{uid}/memory.json`, `{base_dir}/users/{uid}/agents/{agent}/facts/*.md`, `{base_dir}/.retrieval/` FTS5 SQLite | Runtime | Yes | Yes | **DEFER** (Future) | active config `memory.manager_class: deermem` + `backend_config.storage_class: file` (`config.yaml:162-170`); the layer is **already pluggable** — `MemoryManager` registry (`manager.py:626-672`) and backends `{noop, openviking (HTTP), honcho, mem0, deermem}`; `storage.py:1830` resolves `storage_class` by reflection | **Medium** — while `storage_class: file` it blocks "no local disk"; per the task, Memory architecture is out of Phase 5. The fix is a backend selection, not a new abstraction |
| 8 | **User custom skills + per-user skill enabled state** | `app/gateway/routers/skills.py` (`_get_user_skill_storage` L176-182, install-from-archive L244, `write_custom_skill` L471); `skills/storage/user_scoped_skill_storage.py` L90 (`_skill_states.json`), `skills/storage/local_skill_storage.py` | `{base_dir}/users/{uid}/skills/custom/`, `{base_dir}/users/{uid}/skills/_skill_states.json` | Runtime | Yes | Yes | **MOVE_TO_OBJECT_STORE** | skill CRUD API + archive install; `sandbox/tools.py:229-274` reads `_skill_states.json` to hide disabled skills from the agent; `skills_view/` is only a deferred Sandbox projection | **Medium** — `SkillStorage` ABC + `skills.use` reflection factory already exist (`skills/storage/skill_storage.py`, `skills/storage/__init__.py:39-71`), so this is a new backend, not a new abstraction |
| 9 | **Bundled / public skills (source of truth)** | `skills/` at repo root; `config/skills_config.py`; helm mounts `/app/skills` read-only | repo `/app/skills` | Static | N/A | No | **KEEP_LOCAL** | helm `gateway-deployment.yaml` mounts `name: skills` at `/app/skills` `readOnly: true` | Low |
| 10 | **Skill projection cache (`skills_view/`)** | `skills/projection.py` (`get_skill_projection_paths` L52, `_copy` L126, `copytree` L170); `config/paths.py::skills_view_dir` L273, `public_skills_view_dir` L278, `user_skills_view_dir` L282 | `{base_dir}/skills_view/`, `{base_dir}/users/{uid}/skills_view/` | Runtime, **derived/rebuildable** | No | No | **DEFER_TO_SANDBOX** | `projection.py:1` "Materialize enabled-only skill trees for **sandbox filesystem exposure**"; consumed only as a sandbox mount (`local_sandbox_provider.py:167`, `paths.py:306-308`); old inventory already placed this in Phase 6 (step 6.2) | Low |
| 11 | **Config / extensions config** | `config.yaml`, `extensions_config.json`; `config/extensions_config.py` (`resolve_config_path` L490, `atomic_write_extensions_config`) | repo root + helm-seeded home volume | Static (runtime-mutable) | N/A | No | **KEEP_LOCAL** | helm initContainer seeds it into the writable home volume (`gateway-deployment.yaml` `init-extensions`) | Low |
| 12 | **Channel connection store (JSON)** | `app/channels/store.py` L38-42; constructed **unconditionally** at `app/channels/service.py:150` (`self.store = ChannelStore()`) | `{base_dir}/channels/store.json` (+ `channels/` dir created at every Gateway start) | Runtime | Yes | Yes | **DEFER — separate connection/config persistence** | dir observed on disk: `backend/.deer-flow/channels/`; `ChannelService` built from `start_channel_service` (`app/gateway/app.py:332`) | **Medium** — later evaluate DB persistence; do not move this JSON into Phase 5 Object Storage |
| 13 | **Channel runtime config store (JSON, credentials)** | `app/channels/runtime_config_store.py` L27-31 (`chmod 0o600`), `merge_runtime_channel_configs` L111-131 | `{base_dir}/channels/runtime-config.json` | Runtime | Yes | Yes | **DEFER — separate connection/config persistence** | gated by `channel_connections.enabled` (L118); active config `channel_connections.enabled: false` (`config.yaml:254-255`), so dormant but reachable | **Medium** — provider credentials belong to a later Credential Broker / secret-persistence decision, not Object Storage |
| 14 | **Channel attachment uploads / artifact read** | `app/channels/manager.py` L732 (`resolve_outputs_confined_path`), L797-859 (writes `user-data/uploads/`); `app/channels/sandbox_files.py:24-25` (mount-based providers skip the sync) | rows 1–2 | Runtime | Yes | Yes | **DEFER — Channel Attachment redesign** | `ChannelManager` instantiated at `service.py:158-170`; current calls are known future integration points, but no transport, attachment model, or Channel/Sandbox protocol is selected in Phase 5 | **Medium** — must be revisited when custom Channel attachment support is implemented; not a Phase 5 Object Storage task |
| 15 | **`USER.md` global user profile** | `config/paths.py::user_md_file` L169; `app/gateway/routers/agents.py:500-540` (`GET`/`PUT /api/agents/user-profile`) | `{base_dir}/USER.md` | Runtime | Yes | Yes | **DELETE** | **No injection consumer exists**: `user_md_file` has exactly one reader/writer (`routers/agents.py:515,529`); grep across `harness/deerflow` for `user_md`/`USER_MD` returns only the `paths.py` definition; no frontend consumer (`grep -rn "user-profile" frontend/src` → 0); no docs; one test (`backend/tests/test_custom_agent.py`) | **Medium** — removal deletes the obsolete public endpoint (`agents_api.enabled: true`, `config.yaml:204`). The docstring claim "injected into all agents" (`paths.py:109`) has no implementation |
| 16 | **JWT signing secret** | `app/gateway/auth/config.py::_load_or_create_secret` L36-57 | `{base_dir}/.jwt_secret` (0600) only as local fallback | Runtime (bootstrap) | Yes | **Yes** | **KEEP_LOCAL — production requires `AUTH_JWT_SECRET`** | L68-70 reads `AUTH_JWT_SECRET` first and only falls back to the file; local fallback remains for single-node/development | **Resolved for Phase 5 boundary** — production configuration, not Object Storage, supplies one shared signing secret |
| 17 | **Admin initial credentials** | `app/gateway/auth/credential_file.py::write_initial_credentials` L24-52 | `{base_dir}/admin_initial_credentials.txt` (0600) | Runtime (operator one-shot) | No | No | **KEEP_LOCAL** | only consumer is `app/gateway/auth/reset_admin.py:21` (operator CLI); purpose is to keep the secret out of logs | Low |
| 18 | **Thread branch workspace-copy guard** | `app/gateway/routers/threads.py::_ignore_branch_user_data` L314-323 | — | Runtime | — | — | folded into row 6 | skips `.upload-*.part` staging files and symlinks | Low |
| 19 | **Task-continuity history (SQLite)** | `agents/task_continuity/archive.py` L49 (`{thread}/task-history/history.sqlite`), L116 (`mkdir 0o700`) | `{…}/task-history/history.sqlite` | Runtime | Yes | Yes (if enabled) | **DEFER** | `task_continuity.enabled: false` (`task_continuity_config.py:7`, `config.yaml:138`); production consumers only via `agents/task_continuity/tools.py:97` + `client.py:403` | Low while disabled — natural fix is the existing SQL persistence layer, not object storage |
| 20 | **DB / checkpointer SQLite option** | `runtime/store/_sqlite_utils.py` (`resolve_sqlite_conn_str` L11, `ensure_sqlite_parent_dir` L25); `runtime/store/provider.py:113-120`; `runtime/checkpointer/provider.py:118-125` | `{sqlite_dir}/deerflow.db` (default `.deer-flow/data`) | Runtime | Yes | Yes (only if selected) | **DEFER — checkpoint/storage decision outside Phase 5** | `database_config.py:145` "'sqlite' for single-node deployment"; active `database.backend: postgres`; `app/gateway/deps.py:96-97` refuses non-postgres with `GATEWAY_WORKERS>1` | Phase 5 records no checkpoint migration; evaluate real checkpoint metrics separately |
| 21 | **Run events JSONL option** | `runtime/events/store/jsonl.py` (docstring: `.deer-flow/threads/{tid}/runs/{rid}.jsonl`) | local file | Runtime | Yes | No (documented single-process) | **KEEP_LOCAL** | module docstring "**Single-process guarantee** … Use `DbRunEventStore` for multi-process"; `deps.py:99-104` refuses non-`db` for multi-worker; active `run_events.backend: db` | Low |
| 22 | **Agent / managed-subagent definition file backend** | `persistence/agents/file.py` (`user_agent_dir` L148, atomic `os.replace` L249, `rmtree` L142/157/176); `persistence/managed_subagents/file.py:30,39,74`; `config/paths.py::managed_subagents_dir` L184 | `{base_dir}/users/{uid}/agents/{name}/`, `{base_dir}/managed-subagents/` | Runtime | Yes | Yes (only if selected) | **KEEP_LOCAL** | `agent_storage_config.py:6-14` documents both backends and states `file` is "node-local without a shared mount"; active `agent_storage.backend: db` (`config.yaml:225`); `deps.py::_validate_agent_storage` L116+ | Low |
| 23 | **Extensions install / source snapshot** | `extensions/manager.py` (L123 `project_root`, L160 `backend/extensions/sources`, `copytree` L213, `rmtree` L270/290/292/294/408/432, `subprocess` uv at L12) | repo `backend/extensions/sources/`, `pyproject.toml`, `uv.lock` | Build / deploy-time | N/A | N/A | **KEEP_LOCAL** | `backend/packages/harness/deerflow/extensions/AGENTS.md` — every mutation requires a Gateway restart | Low |
| 24 | **Runtime state written into the host path tree** | see rows 1–4, 10, 12–15, 19, 22 | `backend/.deer-flow/` observed contents: `.jwt_secret`, `.retrieval/`, `.skills_view.projection.lock`, `channels/`, `data/deerflow.db`, `skills_view/`, `users/` | Runtime | Yes | Yes | covered by rows above | direct `ls` of `backend/.deer-flow/` | — |
| 25 | **(non-production)** TUI-era embedded client upload copy | `harness/deerflow/client.py:1632` (`shutil.copy2` into `uploads_dir`) | `{…}/user-data/uploads/` | Runtime, **debug-only** | No | No | **non-production — out of scope** | `client.py:1-9` self-describes as an embedded client; its only non-test consumer was the TUI, which Phase 2 deleted (no `deerflow/tui` files in HEAD). Sole remaining caller: `backend/scripts/e2e_safety_termination_demo.py:107` | Low |

---

## 2. Obsolete Phase 5 Items

### 2.1 Old Phase 5 steps that no longer hold at HEAD

| Old step | Old claim | Status at HEAD | Why |
|---|---|---|---|
| **5.4** — "Memory 新增非文件 backend（对象存储或 DB）" | D4: memory hard-codes file paths | **OBSOLETE — move to Future** | The abstraction already exists. `MemoryStorage` is an ABC resolved by reflection (`deermem/core/storage.py:1830-1853`) and `MemoryManager` is a registry (`agents/memory/manager.py:626-672`); backends already shipped: `noop`, `openviking` (**HTTP**), `honcho`, `mem0`, `deermem`. Nothing new needs to be *built* in Phase 5 — only selected. Old D4's justification ("存储实现硬编码文件路径") is invalidated by the pluggable design. |
| **5.6** — "引入 Agent Workspace abstraction" | D20: workspace abstraction missing | **OBSOLETE — move to Phase 6** | The task classifies the agent workspace as DEFER_TO_SANDBOX. Old inventory itself already placed the adjacent skills-projection decoupling in Phase 6 (step 6.2). No Phase 5 consumer requires a workspace abstraction. |
| **5.7** — "租户级存储隔离（独立 Bucket / 独立 DB）" | D23 | **OBSOLETE — move to Future** | Task explicitly excludes Tenant physical isolation. Old inventory itself said the form was "待存储架构设计阶段确定" (unresolved). Not Phase 5. |
| **5.3** — "收敛 `config/paths.py` 调用面到 storage 接口后面（改动面最大）" | D8: `paths.py` is an implicit global hub | **PARTIALLY OBSOLETE — narrow it** | The premise is still true in breadth (`paths` is imported by ~100 production modules), but the *scope* is now much smaller: `paths.py` is mostly `Path` arithmetic, and the Phase 5 runtime-persistent surfaces are outputs / uploads / `.tool-results` and Custom Skills/state (rows 1–3, 8). Workspace, `acp-workspace`, `skills_view` and the Sandbox mount helpers stay local by design. Old 5.3's "收敛**全部**调用面" would be a future-only abstraction with no consumer — which the task forbids. |
| **5.1** — "引入 ArtifactStore 抽象" | D5: artifacts have no abstraction | **VALID but re-scope** | Artifacts genuinely lack an abstraction and genuinely have consumers. Keep — but scope it to the call sites that actually exist (`artifacts.py`, `path_utils.py`, `artifact_archive.py`, `present_file_tool.py`, `run_worker` delivery, and Channel attachment reads), not a general-purpose store. |
| **5.2** — "引入 Object Storage 适配层（S3 兼容），Artifact / Uploads 先接入" | D7: object storage missing | **VALID, broadened** | Still zero object-storage code in production (grep for `boto3|minio|oss2|ObjectStore|object_storage|S3Storage|s3_client|fsspec` across `backend/` returns only a test string). The adapter must serve outputs/artifacts, uploads, `.tool-results`, and the non-local Custom Skill backend. |
| **5.5** — "`tool_output` 外置改为对象存储或中间截断" | D16 | **VALID — externalization retained** | Location changed: `.tool-results` is now **under `outputs/`** (P3), not at the thread root. The shared object store is required; truncation is not the Phase 5 resolution. |

### 2.2 Old Phase 5 items whose *filesystem scope* disappeared

| Removed by | Filesystem scope that disappeared | Evidence |
|---|---|---|
| **Phase 3** (`9e9f1888`) | GitHub artifact/file handling, `gateway/github/*`, `routers/github_webhooks.py`, `routers/integrations.py`, `integrations/lark_broker.py`, `integrations/lark_cli.py`; the `integrations/lark-cli/{config,data}` subtree from old §8.2 | `git ls-tree -r HEAD -- backend/app/gateway/github` → empty; `backend/packages/harness/deerflow/integrations/` → only `__init__.py`; `git show --stat 9e9f1888` lists those deletions |
| **Phase 4** (`cfd41bf6`) | `BROWSER_FRAMES_DIRNAME` and the browser frames directory under outputs; `community/browser_automation/*`; `utils/url_safety.py`; `routers/browser.py`; `browser_capability.py` | `git show cfd41bf6 -- backend/app/gateway/artifact_archive.py` removes `BROWSER_FRAMES_DIRNAME` from the reserved-name set; `constants.py` lost the constant |
| **Phase 2** (`432e2c09`) | Frontend workspace-changes consumers; TUI (`deerflow/tui/*`, `docs/TUI.md`, all `test_tui_*`); community sandbox providers `boxlite`/`e2b_sandbox`/`tenki`; search/scrape tool packs (`brave`, `browserless`, `crawl4ai`, `ddg_search`, `exa`, `firecrawl`, `jina_ai`, `searxng`, `serper`, `serply`, `sofya`, `tavily`, `tencent_wsa`, `image_search`, `ragflow`, `lightrag`, `infoquest`, `groundroute`) | `git show --stat 432e2c09` (184 files, −37,021 lines) |

### 2.3 Old items with no production consumer → delete, do not migrate

| Item | Evidence | Disposition |
|---|---|---|
| `USER.md` global user profile (row 15) | The documented injection consumer does not exist. `user_md_file` has exactly one reader/writer pair (`routers/agents.py:515,529`); no `harness/` code reads it; no frontend consumer; no docs; one test. | **DELETE** `USER.md` and the `GET/PUT /api/agents/user-profile` endpoint; the confirmed boundary does not preserve an unused endpoint contract. |
| `persistence/webhook_delivery/{__init__,model}.py` | The SQL store was deleted in Phase 3; the ORM model survives and is consumed only by `app/channels/dedupe_store.py:194-195` and registered in `persistence/models/__init__.py:36`. | **Not a filesystem item** (DB ORM). Retained by explicit decision — `app/channels/AGENTS.md:30-31` ("tables `channel_connections` and `webhook_delivery` are retained for existing data and future extensions"). It belongs to the permanent Channel extension/persistence surface, not Phase 5 Object Storage. |
| `{base}/threads/{thread_id}` legacy (non-user-scoped) layout | Still supported by `config/paths.py:310-328` (`thread_dir` without `user_id`) and `paths.py:377`. Production callers all pass `user_id`; the legacy branch is reached only when `user_id is None`. | **DEFER** — not a storage-target question. Legacy-layout cleanup belongs to a migration/compat task, not Phase 5. |
| `agents_dir` / `agent_dir` / `agent_memory_file` legacy properties | `config/paths.py:173-202`; the docstring itself says "New code should use `user_agents_dir` instead … remains only as a read-side fallback for installations that have not yet run `migrate_user_isolation.py`". | **DEFER** — migration-compat, not Phase 5. |

---

## 3. Proposed Phase 5 Scope

### 3.1 Phase 5 必须做

Only changes for the narrowed Phase 5 goal: the listed runtime-persistent objects must be readable and mutable across instances without relying on instance-local persistent disk. This does not mean that every application filesystem path becomes shared or disappears.

| # | Change | Targets | Blocks which goal |
|---|---|---|---|
| **A** | Give the unified **outputs / artifacts** namespace a narrow S3-compatible Storage Port and route the existing call sites through it: `routers/artifacts.py` (get, update, reserve), `path_utils.py::resolve_outputs_confined_path`, `artifact_archive.py::build_artifact_archive`, `tools/builtins/present_file_tool.py`, and `run_worker` output verification. Support only the required streaming write/read, range read, stat/metadata, size, MIME, checksum, list, and delete operations; do not create a POSIX filesystem abstraction or separate Artifact/Output stores. | rows 1 and 3 | cross-instance outputs/artifact access |
| **B** | Give **uploads** a narrow S3-compatible Storage Port: `routers/uploads.py` (streamed write, limits, list, delete) and `uploads/manager.py` (naming/path semantics and listing). Stream request data directly to Object Storage; do not make Agent Server local staging a normal path. Preserve existing same-name and `claim_unique_filename` behaviour. | row 2 | cross-instance upload access |
| **C** | Keep **large tool output** externalization and resolve `.tool-results` through the same shared outputs/object-storage port. Later turns must be able to read the externalized result by reference; do not replace this with truncation or put the large payload in Agent state/checkpoints. | row 3 | cross-instance tool-result readability and bounded runtime state |
| **D** | Re-express the **Phase 5 persistent-object lifecycle** against the storage ports: branch copy and delete for thread-scoped outputs, artifacts, uploads, and `.tool-results`, plus the applicable delete/state lifecycle for Custom Skills. Preserve existing branch inheritance and simple synchronous delete semantics; do not add tombstones, asynchronous GC, or local `copytree`/`unlink`/`rmtree` operations for persistent objects. | row 6 | correctness after A/B/C/E; no local lifecycle operations for Phase 5 objects |
| **E** | Route **user custom skills + `_skill_states.json`** through a shared non-local `SkillStorage` backend using the existing ABC and reflection factory. Keep `skills_view` as an instance-local, derived, disposable projection that can be rebuilt on demand from Shared SkillStorage; do not add TTL, distributed invalidation, write-back, or cache locks. Bundled/Public Skills remain static. | row 8 | cross-instance skill CRUD, enabled-state consistency, and projection rebuildability |
| **F** | Close the **Phase 5 storage configuration gate**: validate that outputs/artifacts, uploads, `.tool-results`, and custom skills use shared backends before allowing the multi-instance acceptance test. Do not fold Memory, Sandbox workspace, Channel attachment redesign, Channel connection/config JSON, or checkpoint design into this gate. | rows 1–3, 8 | prevents a multi-replica deploy from silently losing Phase 5 objects |
| **G** | Delete `USER.md`, `config/paths.py::user_md_file`, and `GET/PUT /api/agents/user-profile`, including their obsolete test/route surface. | row 15 | removes an unconsumed runtime file instead of migrating it |
| **H** | Make `AUTH_JWT_SECRET` an explicit production deployment requirement. Keep `.jwt_secret` only as a single-node/local-development fallback; do not add Object Storage support for it. | row 16 | cross-instance JWT verification without production file persistence |

### 3.2 Phase 5 明确不做

- **Sandbox final implementation** — including `sandbox/tools.py` splitting (old D2 / step 6.1), LocalSandbox→remote default (old D1 / step 6.3), Agent-level tool policy (old D12 / step 6.4), and MCP stdio workspace injection (old D17 / step 6.5). All remain Phase 6.
- **Remote execution for the temporary workspace** — `user-data/workspace/` and `acp-workspace/` (rows 4–5) stay `DEFER_TO_SANDBOX`.
- **Long-term Memory architecture rework** (row 7). The backend abstraction already exists; the only Phase-5-relevant question is *which backend is configured*, which is a deployment decision.
- **Tenant physical isolation** (old D23 / step 5.7).
- **Static files unrelated to persistent objects** — bundled skills, `config.yaml`, `extensions_config.json`, extension source snapshots, helm-mounted skills (rows 9, 11, 23).
- **Future-only abstraction** — no general-purpose `storage/` layer over all of `config/paths.py`; no `ArtifactStore` beyond the call sites that exist; no workspace abstraction.
- **Data paths with no current consumer** — `USER.md` (row 15) is deleted in Phase 5, not migrated.
- **Channel connection/config persistence** — `channels/store.json` and `channels/runtime-config.json` (rows 12–13) are not Object Storage types. Handle them later with DB / Credential Broker decisions.
- **Channel Attachment redesign** — row 14 remains a known future caller of the shared Upload/Artifact namespaces, but Phase 5 does not select an internal HTTP protocol, attachment transport, `ResolvedAttachment` model, or Channel/Sandbox interaction.
- **Checkpoint storage design** — no checkpoint migration or checkpoint database change is included in Phase 5. Evaluate it separately after measuring real checkpoint behaviour.
- **Historical data migration** — no backfill, dual-read, dual-write, migration script, rollback compatibility, or migration orphan cleanup; the cutover is fresh because there is no old local data to preserve.
- **General filesystem abstraction** — no POSIX-compatible wrapper around Object Storage and no Agent Server persistent staging layer.
- **Explicitly retained single-node options** — SQLite DB, JSONL run events, and file-backed agent definitions (rows 20–22) remain documented options; they are not Phase 5 object-storage targets.

### 3.3 Shared Object Storage Scope

Data types that genuinely need shared object storage, with their current consumers. No interface design, no implementation.

| Data type | Current consumers (evidence) | Why shared storage |
|---|---|---|
| **Thread outputs / artifacts** (`user-data/outputs/**`) | `routers/artifacts.py` GET/PUT; `artifact_archive.py` archive; `present_file_tool.py`; frontend `core/artifacts/utils.ts`; `run_worker` produced-output detection | A run may execute on any instance; the user downloads the artifact from whichever instance nginx routes to |
| **Thread uploads** (`user-data/uploads/**`) | `routers/uploads.py`; `uploads_middleware.py:229`; `list_uploaded_files_tool.py:129`; frontend `core/uploads/api.ts` | Upload lands on one instance, while the run may be served by another |
| **Large tool output** (`user-data/outputs/.tool-results/**`) | `tool_output_budget_middleware.py`; model `read_file` by reference on later turns; excluded by `artifact_archive.py:243` and `workspace_changes/scanner.py` | Written during a run, read back on later turns that may resume on a different instance; externalization avoids large Agent state/checkpoint payloads |
| **User custom skills + per-user enabled state** | `routers/skills.py` CRUD; `SkillStorage` factory; runtime skill lookup/state reads | User-authored content and enabled state must be identical on every instance |

Explicitly **not** in this list: workspace / `acp-workspace` (Sandbox execution dirs), `skills_view` (rebuildable projection), Memory (out of scope, pluggable), Channel attachments (redesign deferred), Channel connection/config JSON (separate persistence), `USER.md` (deleted), JWT secret (deployment secret), and checkpoints (separate future evaluation).

### 3.4 Confirmed implementation constraints

| Area | Phase 5 decision |
|---|---|
| Object Storage | Use an S3-compatible API with configurable `endpoint`, `bucket`, `region`, `prefix`, access key/secret key, and optional server-side encryption. Do not expose `path-style` or TLS as Phase 5 configuration knobs; the agreed deployment addresses use HTTP. Credentials come from deployment Secrets/environment variables, never repository config. |
| Verification Object Storage | Add MinIO to both standard stacks (`docker/docker-compose.yaml` and `docker/docker-compose-dev.yaml`), persist it with a named volume, and add a MinIO healthcheck. Do not add application bucket initialization; bucket existence is an external test/deployment precondition. Tests that depend on MinIO wait for the service to become healthy and use an empty bucket for fresh cutover. |
| S3 client | Use `aiobotocore` as the async S3-compatible client, with streaming support. Do not call a blocking S3 SDK directly on the FastAPI event loop. |
| Key layout | Preserve business namespace semantics: `{prefix}/users/{user_id}/threads/{thread_id}/outputs/{relative_path}`, the corresponding `uploads/{filename}`, `outputs/.tool-results/{name}`, and user-scoped `skills/custom/{skill_name}/...` plus `_skill_states.json`. No tenant-specific physical bucket isolation and no POSIX filesystem wrapper. |
| Storage Port | Implement only the narrow operations required by current callers: streaming write/read, range read, stat/metadata, size, MIME, checksum, list, and delete. Outputs and artifacts share one namespace/port; `.tool-results` remains internal data under outputs, not a separate Artifact type. |
| Agent Server disk | Normal paths stream directly between request/response and Object Storage. Do not build a persistent local staging abstraction. A real local path, when required by execution, is a Remote Sandbox concern and is outside this Phase's redesign. |
| Existing data | Fresh cutover only, validated against an empty MinIO bucket. No backfill, dual-read, dual-write, migration script, historical rollback compatibility, or migration orphan cleanup is required because no old local data must be preserved. |
| Upload semantics | Keep the existing Upload API, same-name behavior, and `claim_unique_filename` collision rules. Replace only the persistence backend. |
| Custom Skills | Shared non-local `SkillStorage` is the source of truth. `skills_view` remains instance-local, derived, disposable, and rebuildable on demand; no TTL, distributed invalidation, write-back, or distributed cache lock. |
| Channel | Channel framework stays available, but attachment transport and Channel/Sandbox interaction are deferred; see [`phase5-channel-fs-decoupling.md`](phase5-channel-fs-decoupling.md). |

---

## 4. Deferred Scope

### Phase 6 — Sandbox / Execution Environment / temporary workspace / shell & code-execution filesystem

| Item | Location | Note |
|---|---|---|
| Agent workspace | `paths.sandbox_work_dir` L330; `aio_sandbox_provider.py:983` | thread-scoped execution directory, mounted into the sandbox; outside the narrowed Phase 5 completion condition |
| ACP workspace | `paths.acp_workspace_dir` L354; `invoke_acp_agent_tool.py:42` | same lifetime rule |
| Skill projection (`skills_view/`) | `skills/projection.py`; `local_sandbox_provider.py:167` | derived, rebuildable, exists only for sandbox exposure |
| MCP stdio pinned workspace + temp dir | `mcp/tools.py::_prepare_stdio_workspace` L200-217 | `tmp_dir = workspace/MCP_TMP_SUBDIR` |
| Sandbox filesystem tools | `sandbox/tools.py` (2,588 lines: `ls`/`glob`/`grep`/`read_file`/`write_file`/`str_replace`) | old D2 / step 6.1 — still one module for execution primitives *and* tool definitions |
| LocalSandbox as default | `config.yaml:100`, `config.example.yaml:981` → `deerflow.sandbox.local:LocalSandboxProvider` | old D1 / step 6.3 — unchanged at HEAD |
| Workspace-change scanning | `workspace_changes/recorder.py` L27-49, `scanner.py:134` (`os.walk`) | reads the workspace + outputs trees pre/post run; feeds run delivery |
| Workspace snapshot temp dir | `workspace_changes/recorder.py:48` (`tempfile.mkdtemp`) | ephemeral, cleaned in `finally` |

### Future — Tenant physical isolation / long-term Memory evolution / undetermined lifecycle / long-term storage problems not currently worth solving

| Item | Classification | Note |
|---|---|---|
| Long-term Memory storage evolution | **DEFER** | Backend abstraction + non-file backends already exist (`openviking` HTTP, `mem0`, `honcho`, `noop`). Needs a product/deployment decision, not new code. |
| Tenant physical storage isolation | **DEFER** | Old D23 / Q4. Form was never decided; task excludes it. `USER.md` is deleted in Phase 5 rather than fixed by adding tenant scoping. |
| `task-history/history.sqlite` (task continuity) | **DEFER** | Feature disabled (`task_continuity.enabled: false`). If enabled, the natural home is the existing SQL persistence layer, not object storage. |
| Legacy non-user-scoped `{base}/threads/{tid}` layout; `agents_dir`/`agent_dir`/`agent_memory_file` | **DEFER** | Migration/compat fallbacks documented in `paths.py:173-182, 310-328`. |
| Channel framework FS stores (`channels/store.json`, `channels/runtime-config.json`) | **DEFER — separate persistence track** | Channel framework is a permanent extension surface. These files are Channel connection/config persistence: evaluate DB for the connection store and a Credential Broker/secret-persistence mechanism for runtime credentials. Do not classify them as Phase 5 Object Storage data. |
| Checkpoint persistence design | **DEFER — separate evaluation** | Phase 5 does not migrate checkpoints or add checkpoint database tables. After Object Storage is complete, measure average/P95/P99 size, write frequency, per-thread checkpoint count, and large-payload cases before selecting a final backend. |
| SQLite DB / JSONL run events / file-backed agent definitions | **DEFER** | Explicitly documented single-node options, each gated against multi-worker and each superseded by a `db` backend in active use. They are not new Phase 5 migration targets. |

---

## 5. Phase 5 Execution Gate

### Conditions verified

| Condition | Result |
|---|---|
| Every Phase 5 storage item has real production-consumer evidence | **PASS** — rows 1, 2, 3, 6, and 8 cite frontend, agent-side, or lifecycle call chains; Channel attachment remains a separately documented future caller. |
| Every `DELETE` item confirmed to have no production call chain | **PASS** — row 15 (`USER.md`) has no Agent, frontend, or documentation consumer; its GET/PUT endpoint is explicitly removed. |
| `KEEP_LOCAL` items do not affect the narrowed Phase 5 completion condition | **PASS** — JWT is supplied by mandatory production `AUTH_JWT_SECRET`; workspace, Memory, single-node options, and other local files are outside the condition. |
| `DEFER_TO_SANDBOX` contains no data included in the narrowed Phase 5 goal | **PASS** — workspace, ACP workspace, and `skills_view` remain Phase 6 execution/projection concerns. |
| Phase 5 does not include the Phase 6 Sandbox refactor | **PASS** — §3.2 excludes it explicitly; §4 keeps it in Phase 6. |
| Phase 5 scope converged on current HEAD and the confirmed Channel boundary | **PASS** — Channel is a permanent extension surface; attachment transport/redesign is deferred, while connection/config JSON is a separate persistence track. |
| `.tool-results` remains externalized and is not placed in Agent state/checkpoints | **PASS** — it uses the shared outputs/object-storage path and remains readable by reference in later turns. |
| Checkpoint migration is excluded | **PASS** — no checkpoint schema/storage migration is proposed; evaluation is explicitly deferred until after Phase 5. |
| No new storage abstraction created for a deleted capability | **PASS** — §2.1 removes obsolete work; §3.2 forbids a general-purpose abstraction; `SkillStorage` is reused for Custom Skills; `USER.md` is deleted rather than migrated. |

### Required multi-instance acceptance

The implementation is complete only after a real two-Agent-Server-instance test against the Docker-stack MinIO service, not only unit tests. The MinIO service must be healthy before the dependent tests start; the test bucket starts empty.

| Area | Required result |
|---|---|
| Outputs / Artifacts | Instance A writes an artifact; instance B can list, read, and range-read it; after B updates it, A reads the latest content. |
| Uploads | Instance A uploads; B can list/read/use it; restarting A or removing its local files does not affect the upload. |
| `.tool-results` | A externalizes a large Tool/MCP result; a later turn/run on B reads the complete result by logical reference. |
| Custom Skills | A creates/updates a Custom Skill and enable state; B sees the same data; with no local `skills_view`, B rebuilds it from Shared SkillStorage; deleting the projection does not lose the source. |
| Branch / Delete | A creates outputs/uploads; B performs branch/delete; A sees the same inheritance and deletion results without consulting B's local directory. |
| Statelessness | The Source of Truth for outputs/artifacts, uploads, `.tool-results`, Custom Skills, and Skill enable state is not Agent Server local persistent disk. |

### Verdict

```
READY FOR PHASE 5 IMPLEMENTATION
```

This verdict is limited to the confirmed scope and planning boundary. No Phase 5 code has been implemented in this document.

### Blocking Issues

No active boundary blocker remains after the confirmed decisions. The former blockers are resolved as follows:

**B1 — RESOLVED: Channel framework is retained intentionally.**
`backend/app/channels/` is a permanent extension surface, not incomplete Phase 3 work. Its current attachment upload and artifact-read paths are recorded as future callers, but Phase 5 does not redesign that transport or choose a Channel/Sandbox protocol. `channels/store.json` and `channels/runtime-config.json` remain outside Object Storage and are assigned to later DB / Credential Broker persistence work.

**B2 — RESOLVED: JWT signing secret is a deployment secret, not an Object Storage object.**
Production deployments must provide `AUTH_JWT_SECRET` consistently to every instance. `.jwt_secret` remains only as a single-node/local-development fallback; Phase 5 adds no migration for it.

**B3 — RESOLVED: Phase 5 completion is intentionally narrowed.**
Phase 5 covers only outputs/artifacts, uploads, externalized `.tool-results`, Custom Skills and enabled state, their storage-aware lifecycle, the rebuildable `skills_view` projection, and `USER.md` deletion. Agent workspace, ACP workspace, Sandbox filesystem, Remote/LocalSandbox architecture, MCP stdio workspace, Memory, Channel Attachment redesign, tenant physical isolation, historical data migration, and checkpoint storage are not completion criteria for this Phase.

The remaining work is implementation work within the approved scope: select/configure the shared object-storage backend, add the non-local `SkillStorage` backend, migrate the listed callers and lifecycle operations, enforce the production JWT prerequisite, and remove the confirmed `USER.md` endpoint/file surface.

---

## 6. Non-blocking observations (for the record)

- **Old inventory §8.2 directory tree is stale** in at least six ways: `.tool-results` moved under `outputs/` (P3); `integrations/lark-cli/` is gone (Phase 3); `channels/` now exists at the base root; `managed-subagents/` is absent from the tree; `USER.md` and `{thread}/task-history/` are absent; `{base}/threads/{tid}` legacy path is absent. Any Phase 5 work should use `config/paths.py` as the source of truth, not §8.2.
- **Old inventory §1.3 / R2 ("默认部署不是无状态的") is outdated** — `database.backend: postgres`, `run_events.backend: db`, `agent_storage.backend: db`, `stream_bridge.type: redis` are all already in place (P2).
- **Old inventory §8.3 "Uploads → 落 uploads/，再 `sandbox.update_file()` 同步"** is provider-dependent: `app/channels/sandbox_files.py:24-25` returns early for mount-based providers (`uses_thread_data_mounts`), so the sync only happens for non-mount providers. Channel attachment handling remains deferred; no Phase 5 transport or materialization protocol is selected.
- **The old §8.1 "Object Storage / S3 ❌ 不存在" conclusion still holds** — re-verified at HEAD.
- **`workspace_changes` is not dead code** despite its frontend consumers being deleted in Phase 2 (P4) — it is called from `runtime/runs/worker.py`. Do not schedule it for deletion on the basis of the missing UI.
