# PostgreSQL → MySQL 8.0.24 迁移可行性分析

> **文档类型**：可行性调查 + 迁移设计（**不含任何代码/Schema 变更**）
> **调查对象**：`deer-flow` 仓库当前 HEAD（`backend/` 为主）
> **调查日期**：2026-09-16
> **目标数据库**：MySQL 8.0.24 + InnoDB
> **约束**：本任务未修改任何生产代码、Schema、migration、Docker 初始化文件或数据库驱动。
> 所有结论均来自只读代码检索、依赖清单核对与静态分析。

---

## 1. Executive Summary

**整体结论：`Feasible with significant changes`。**

当前项目**可以**移除 PostgreSQL 并落到 MySQL 8.0.24，但**不能**通过"换驱动 + 换方言"完成。真正的工作量集中在两处：

1. **LangGraph 持久化层必须自研或深度定制。** 本项目当前把 checkpoint 与 store 完全交给
   `langgraph-checkpoint-postgres` 3.1.1（`PostgresSaver` / `AsyncPostgresSaver` /
   `PostgresStore` / `AsyncPostgresStore`）。该包的表结构**原生使用 `JSONB` 与 `BYTEA`**，
   且 SQL 里硬编码了 `jsonb_each_text`、`array_agg`、`::bytea` 强转、`ANY(%s)`、
   `CREATE INDEX CONCURRENTLY`、`%s` 占位符。它**没有 MySQL 方言分支**，
   不是"配置一下就能换库"的组件。
   LangGraph 官方**不存在** MySQL checkpointer/store；社区第三方包
   `langgraph-checkpoint-mysql` 虽然功能可用，但其默认 Schema 使用
   `JSON` + `LONGBLOB` + `BINARY(16)`，且表名无 `ag_` 前缀、无中文 COMMENT、
   键长/序列化设计均不符合本项目规范（详见 §16）。

2. **并发正确性语义发生变化，且不是"SQL 能不能跑"的问题。**
   项目在 4 处依赖 PostgreSQL 的 **transaction-level advisory lock**
   （`pg_advisory_xact_lock`）与 **session-level advisory lock**（`pg_advisory_lock`），
   在 1 处依赖 **`INSERT ... ON CONFLICT ... WHERE ... RETURNING`**，
   在 1 处依赖 **`UPDATE ... RETURNING`**。
   MySQL **没有 `RETURNING`**（这是硬阻塞），**没有**与 `pg_advisory_xact_lock`
   语义等价的原语（`GET_LOCK` 是连接级而非事务级，在连接池下需要重新设计），
   **没有 partial index**（需要生成列 workaround）。

**分层结论速览**

| 职责域 | 迁移难度 | 关键阻碍 |
| --- | --- | --- |
| Application Data（20 张 ORM 表） | **Moderate** | 24 个 `JSON` 列、4 处 FK、2 个 partial unique index、`sa.JSON` 路径查询 |
| LangGraph Checkpoint | **Hard**（接近 Replace） | `JSONB`/`BYTEA`/msgpack-pickle 序列化、`jsonb_each_text`、`RETURNING` 无关但 `%s`+`CONCURRENTLY`、TEXT 键长上限 |
| LangGraph Store | **Moderate → Hard** | `value JSON NOT NULL`；使用面很窄但无合规实现 |
| Queue / Scheduler | **Hard** | advisory lock + `UPDATE ... RETURNING` + `SKIP LOCKED` + partial unique index + 隔离级别变化 |
| Knowledge / Vector | **Replace（不需要 MySQL 承担）** | 当前**未使用 pgvector**；已有独立 RAG 服务与本地 FTS5 |

**三个最大风险**（详见 §17）

1. Checkpoint 持久化层没有合规的现成实现，必须自研 saver + 决定 blob 的落地方式
   （TEXT/LONGTEXT 序列化 vs Object Storage 卸载），这是**唯一可能推翻整个方案**的点。
2. 并发原语替换（advisory lock / `RETURNING` / 隔离级别 / gap lock）会改变调度器、
   run 所有权、事件序号分配的失败模式，属于"能跑但可能静默错误"的一类风险。
3. Schema 层面的隐性约束：InnoDB 索引键长上限 3072 字节、`TEXT` 不能作主键、
   `DATETIME` 默认精度 0（会截断亚秒，破坏 FIFO 与租约比较）、
   `DateTime(timezone=True)` 在 MySQL 上**静默丢弃时区**。

**建议**：不要尝试"一次性把 PostgreSQL 换成 MySQL"。推荐按 §19 的顺序，
**先把 Checkpoint 层做成可替换接口并落地一个 MySQL 实现，再迁移 Application Data**。
若无法接受自研 checkpoint 持久化层的成本，则结论应退回到
`Not currently recommended`。

---

## 2. Current Database Architecture

### 2.1 统一后端选择

项目用**一个** `database:` 配置节同时驱动两条独立职责链
（`backend/packages/harness/deerflow/config/database_config.py:142-209`）：

```python
backend: Literal["memory", "sqlite", "postgres"] = "memory"
postgres_url: str = ""
postgres_schema: str = ""
pool_size / pool_recycle / command_timeout
checkpoint_channel_mode: Literal["full", "delta"] = "full"
```

| backend | 应用 ORM（repositories） | LangGraph Checkpointer | LangGraph Store |
| --- | --- | --- | --- |
| `memory` | 内存实现（`get_session_factory()` 返回 `None`） | `InMemorySaver` | `InMemoryStore` |
| `sqlite` | `sqlite+aiosqlite:///{sqlite_dir}/deerflow.db` | `SqliteSaver` / `AsyncSqliteSaver` | `SqliteStore` / `AsyncSqliteStore` |
| `postgres` | `postgresql+asyncpg://…` | `PostgresSaver` / `AsyncPostgresSaver` | `PostgresStore` / `AsyncPostgresStore` |

关键代码证据：

- `database_config.py:282-294` `app_sqlalchemy_url` → 把 `postgresql://` / `postgres://`
  改写成 `postgresql+asyncpg://`。
- `database_config.py:297-319` `app_sync_sqlalchemy_url` → 改写成 `postgresql+psycopg://`
  （供同步 `agent_storage.backend: db` 消费者使用）。
- `persistence/engine.py:117-131` 在 `backend == "postgres"` 时**硬性** `import asyncpg`，
  缺失即抛 `ImportError`（提示安装 `deerflow-harness[postgres]`）。
- `runtime/checkpointer/provider.py:132-147` 与 `runtime/checkpointer/async_provider.py:129-140`
  分别构造 `PostgresSaver` / `AsyncPostgresSaver`。
- `runtime/store/provider.py:128-143` 与 `runtime/store/async_provider.py:80-95`
  分别构造 `PostgresStore` / `AsyncPostgresStore`。

### 2.2 两条连接池，不同生命周期

注释明确说明（`database_config.py:14-15`）：Postgres 模式下 checkpointer 与 app ORM
**共用同一个数据库 URL，但持有各自独立的连接池**。

- 应用侧：SQLAlchemy `AsyncEngine` + `asyncpg`（`persistence/engine.py:169-182`），
  `pool_size` / `pool_pre_ping` / `pool_recycle` / `command_timeout`。
- Checkpointer/Store 侧：`psycopg_pool.AsyncConnectionPool`
  （`runtime/checkpointer/async_provider.py:51-76`），带
  `autocommit=True`、`prepare_threshold=0`、`row_factory=dict_row`、
  TCP keepalive 参数、`check_connection`。
- 同步路径（TUI / CLI / `DeerFlowClient`）：`PostgresSaver.from_conn_string()` 直连。

### 2.3 当前实际部署形态

- 本地 `config.yaml:223-232` 实际配置为 `backend: sqlite`。
- `deploy/helm/deer-flow/values.yaml:119-149` 默认 `postgresql.enabled: true`，
  捆绑 `postgres:16` StatefulSet
  （`deploy/helm/deer-flow/templates/postgres-statefulset.yaml`），
  探针为 `pg_isready`。
- `docker/docker-compose*.yaml` **不含** postgres 服务（默认 sqlite）。
- **没有任何 SQL 初始化脚本**：Schema 由启动期 `Base.metadata.create_all()` +
  Alembic `stamp` / `upgrade` 生成（`persistence/bootstrap.py:603-701`）。

> ⚠️ 这一点直接影响本任务 §7 的 Migration 规范落地方式，见 §7.2。

### 2.4 Schema 命名现状

**所有表名均无前缀**，包括应用表与 LangGraph 自有表。
Alembic 版本表名为默认的 `alembic_version`
（`persistence/bootstrap.py:361` 直接 `SELECT version_num FROM alembic_version`）。

---

## 3. PostgreSQL Usage Inventory

以下为**实际代码扫描结果**（非 README 推断），按类别列出。

### 3.1 Driver / 连接

| 位置 | 证据 |
| --- | --- |
| `asyncpg`（应用 ORM 异步驱动） | `pyproject.toml:74` `asyncpg>=0.29`；lock 解析为 **0.31.0**；`persistence/engine.py:117-131` 强制 import |
| `psycopg[binary]`（checkpointer/store/健康检查/bootstrap 锁） | `pyproject.toml:76` `psycopg[binary]>=3.3.3`；lock 解析为 **3.3.3** |
| `psycopg-pool` | `pyproject.toml:77` `>=3.3.0`；lock 解析为 **3.3.0** |
| `langgraph-checkpoint-postgres` | `pyproject.toml:75` `>=3.1.1,<3.2`；lock 解析为 **3.1.1** |
| MySQL 驱动 | **全仓库零命中**（`mysql` / `mariadb` / `pymysql` / `aiomysql` 仅出现在 sandbox 环境变量脱敏名单与测试里，与数据库后端无关） |

### 3.2 URL 改写 / Schema 注入

| 文件 | 职责 |
| --- | --- |
| `config/database_config.py:282-319` | `+asyncpg` / `+psycopg` 方言后缀注入 |
| `persistence/postgres_schema.py`（全文 258 行） | `CREATE SCHEMA IF NOT EXISTS`、asyncpg `server_settings.search_path`、libpq `options=-c search_path=` 的**分词/合并/转义**实现、`normalize_libpq_dsn` |
| `config/postgres_schema.py` | `POSTGRES_SCHEMA_PATTERN` 与 `validate_postgres_schema` |

`postgres_schema.py` 里包含一个完整的 libpq `options` tokenizer
（`_split_libpq_options` / `_join_libpq_options` / `_merge_search_path_option`，
`postgres_schema.py:46-126`），这是**纯 PostgreSQL/libpq 语义**，MySQL 没有对应概念。

### 3.3 ORM / Raw SQL / Repository

- **ORM Base**：`persistence/base.py`（`DeclarativeBase` + 通用 `to_dict()`）。
- **20 张应用表**，见 §4。
- **Raw SQL（PostgreSQL 专有）**：

| 文件:行 | SQL | 用途 |
| --- | --- | --- |
| `runtime/events/store/db.py:146-153` | `SELECT pg_advisory_xact_lock(hashtext(CAST(:thread_id AS text))::bigint)` | 事件 `seq` 分配的跨进程串行化（PG 不允许 `SELECT max(...) FOR UPDATE`） |
| `persistence/scheduled_task_runs/sql.py:315-319` | `SELECT pg_advisory_xact_lock(:lock_key)` | 调度器全局并发预算锁（`_SCHEDULER_BUDGET_LOCK_KEY = 4694001`） |
| `persistence/channel_connections/sql.py:397-404` | `SELECT pg_advisory_xact_lock(:lock_key)` | OAuth scope 串行化（key 由 `sha256(owner_user_id\x00provider)` 取 63 bit） |
| `persistence/bootstrap.py:552-561` | `SET LOCAL idle_in_transaction_session_timeout = 0` + `SELECT pg_advisory_lock(:k)` / `pg_advisory_unlock(:k)` | 跨进程 Schema bootstrap 互斥（`_PG_LOCK_KEY = 0x0DEE_12F1_0BEE_3682`） |
| `app/channels/dedupe_store.py:145-172` | `INSERT INTO webhook_deliveries … VALUES (…, now()) ON CONFLICT (…) DO UPDATE SET first_seen = now() WHERE webhook_deliveries.first_seen < now() - make_interval(secs => :ttl) RETURNING channel` + `DELETE … now() - make_interval(secs => :ttl)` | 多 Pod 入站 webhook 去重 |
| `persistence/scheduled_task_runs/sql.py:203-212` | `UPDATE scheduled_tasks SET last_occurrence_seq = last_occurrence_seq + 1 … RETURNING last_occurrence_seq` | 调度 occurrence 序号分配 |

- **SQLAlchemy 方言 import**：
  `persistence/user/preferences.py:4-5` `from sqlalchemy.dialects.postgresql import insert as pg_insert`
  （与 `sqlite_insert` 二选一），`preferences.py:21-25` 使用
  `.on_conflict_do_update(index_elements=["user_id","key"], set_={...})`。

- **JSON 路径表达式**：
  - `persistence/run/sql.py:207` `RunRow.metadata_json["regenerate_from_run_id"].as_string()`
  - `persistence/run/sql.py:228-229` `metadata_json["replay_kind"].as_string()`、`["regenerate_from_run_id"]`
  - `persistence/scheduled_task_runs/sql.py:847` `RunRow.metadata_json["scheduled_task_run_id"].as_string()`
  - 这些在 PostgreSQL 上编译为 `->>`；在 MySQL 上会编译为
    `JSON_UNQUOTE(JSON_EXTRACT(...))` —— **即使 MySQL 允许 JSON，也属于"数据库专有业务逻辑"**。

- **自定义 JSON 比较编译器**：`persistence/json_compat.py`
  （`JsonMatch`，231 行）。为 SQLite 生成 `json_type`/`json_extract`，
  为 PostgreSQL 生成 `json_typeof(...)`/`->`/`->>` + `~ '^-?[0-9]+$'` 正则守卫 +
  `CAST(... AS BIGINT / DOUBLE PRECISION)`。**`_compile_default` 对其它方言直接
  `raise NotImplementedError`**（`json_compat.py:225-227`）。
  使用点：`persistence/thread_meta/sql.py:230,247,261`（线程置顶/归档过滤）。

### 3.4 索引 / 约束（PostgreSQL 专有）

| 对象 | 位置 | PostgreSQL 专有特性 |
| --- | --- | --- |
| `uq_runs_thread_active` | `persistence/run/model.py:56-70` | **partial unique index**：`UNIQUE(thread_id) WHERE status IN ('pending','running')`，同时声明 `sqlite_where` 与 `postgresql_where`。这是"每线程至多一个活跃 run"的**跨进程唯一性保证** |
| `idx_users_oauth_identity` | `persistence/user/model.py:66-84` | **partial unique index**：`UNIQUE(oauth_provider, oauth_id) WHERE oauth_provider IS NOT NULL AND oauth_id IS NOT NULL`；索引名被 `app/gateway/auth/repositories/sqlite.py` 用于**匹配驱动错误里的约束名** |

### 3.5 Docker / Helm / 配置

- `deploy/helm/deer-flow/values.yaml:119-149`：捆绑 `postgres:16`、`database-url` Secret、
  `postgres-password` key。
- `deploy/helm/deer-flow/templates/postgres-statefulset.yaml`：`runAsUser: 999`、
  `pg_isready` readiness/liveness、`volumeClaimTemplates`。
- `deploy/helm/deer-flow/templates/NOTES.txt:75-91`：说明 bundled vs external postgres。
- `config.example.yaml:2286-2343`：`database:` 配置节，含
  `dedupe_storage.backend: auto` 的注释
  （"Postgres whenever database.backend=postgres"）。

### 3.6 Health Check

`app/gateway/health.py:194-215` `_probe_postgres_backend` 直接使用
`psycopg.AsyncConnection.connect(dsn, connect_timeout=…)` + `SELECT 1`，
并复用 `dsn_with_search_path` / `normalize_libpq_dsn`。
`health.py:226-237` 的 `_probe_checkpointer_backend` 只识别
`("sqlite", "postgres")` 两种类型，其它一律 `DATABASE_UNREACHABLE`。

### 3.7 Tests

- 48 个测试文件命中 `postgres`。
- 8 个 PostgreSQL 专属测试文件：
  `test_pg_schema_integration.py`、`test_persistence_bootstrap_pg_lock.py`、
  `test_multi_worker_postgres_gate.py`、`test_scheduled_task_postgres.py`、
  `test_mcp_task_postgres.py`、`test_migration_0018_oauth_identity_pg_partial.py`、
  `test_persistence_engine_postgres_config.py`、`test_postgres_schema_helper.py`。
- 其中 `test_persistence_bootstrap_pg_lock.py:78-98` **逐条断言 SQL 顺序**：
  `SET LOCAL` 必须先于 `pg_advisory_lock`，退出时必须出现 `pg_advisory_unlock`。
- `test_run_event_store.py:498-527` 断言编译结果里包含 `pg_advisory_xact_lock`，
  且**不带 `FOR UPDATE`**。
- `test_thread_meta_repo.py:830-842` 断言 `JsonMatch` 在 MySQL 方言下
  `raise NotImplementedError`。

### 3.8 Scripts

`scripts/migrate_agents_to_db.py`、`scripts/migrate_user_isolation.py`、
`scripts/migrate_memory_markdown.py`、`scripts/benchmark/concurrency/worker.py`
均涉及持久化，但使用的是 ORM/仓储接口，不含 PostgreSQL 专有 SQL。

---

## 4. Current Schema Inventory

### 4.1 物理表清单（PostgreSQL 模式，共 26 张）

**A. 应用表（20 张，由 `Base.metadata` 拥有）**
表清单由 `persistence/bootstrap.py:134-171` 的 `_CANONICAL_0019_SCHEMA_FLOOR`
与各 `model.py` 交叉核对得出：

`agents`、`channel_connections`、`channel_credentials`、`channel_oauth_states`、
`channel_conversations`、`feedback`、`managed_subagents`、`mcp_tasks`、
`personal_access_tokens`、`projects`、`run_events`、`runs`、`scheduled_task_runs`、
`scheduled_tasks`、`subagent_batch_items`、`subagent_batches`、`threads_meta`、
`users`、`user_preferences`、`webhook_deliveries`

**B. 框架表（1 张）**：`alembic_version`

**C. LangGraph 表（5 张，由 `langgraph-checkpoint-postgres` 3.1.1 拥有）**
`checkpoint_migrations`、`checkpoints`、`checkpoint_blobs`、`checkpoint_writes`、`store`
（`_env_filters.py:28-35` 显式声明前 4 张为 LangGraph 所有，Alembic 必须回避。）

### 4.2 `sa.JSON` 列清单（共 24 列）

| 表 | JSON 列 |
| --- | --- |
| `runs` | `metadata_json`、`kwargs_json`、`token_usage_by_model` |
| `run_events` | `event_metadata` |
| `threads_meta` | `metadata_json` |
| `users`（子表 `user_preferences`） | `value` |
| `agents` | `config` |
| `projects` | `presentation` |
| `feedback` | —（无 JSON） |
| `personal_access_tokens` | `scopes` |
| `scheduled_tasks` | `schedule_spec` |
| `mcp_tasks` | `result`、`result_artifact`、`input_required`、`driver_data`、`dispatch_event` |
| `managed_subagents` | `definition` |
| `subagent_batches` | `execution_spec` |
| `subagent_batch_items` | `acceptance_criteria`、`acceptance_verdict`、`token_usage` |
| `channel_connections` | `scopes_json`、`capabilities_json`、`metadata_json` |
| `channel_oauth_states` | `requested_scopes_json`、`metadata_json` |
| `webhook_deliveries` | —（无 JSON） |

### 4.3 外键清单（4 处，迁移后必须移除）

| 位置 | 约束 |
| --- | --- |
| `persistence/user/model.py:37` | `user_preferences.user_id → users.id` `ON DELETE CASCADE` |
| `persistence/channel_connections/model.py:71` | `channel_credentials.connection_id → channel_connections.id` `ON DELETE CASCADE` |
| `persistence/channel_connections/model.py:106` | `channel_conversations.connection_id → channel_connections.id` `ON DELETE CASCADE` |
| `persistence/subagent_batches/model.py:44` | `subagent_batch_items.batch_id → subagent_batches.id` `ON DELETE CASCADE` |

注意：`0001_baseline.py:213,234` 与 `0016_subagent_batches.py:76` 也创建了同样的 FK。
按"历史 migration 不可变"原则，**不修改历史 revision**，而是在新的 MySQL baseline
revision 中不创建 FK，并让 ORM 模型去掉 `ForeignKey`。

### 4.4 nullable 清单（迁移策略见 §7.3）

按语义分类：

| 类别 | 代表列 | 为什么 nullable | 建议 |
| --- | --- | --- | --- |
| **业务上真可选** | `runs.assistant_id`、`runs.error`、`runs.stop_reason`、`threads_meta.display_name`、`users.oauth_provider`/`oauth_id` | 语义允许缺失 | 保持 nullable |
| **所有权回填遗留** | `runs.user_id`、`run_events.user_id`、`threads_meta.user_id`、`feedback.user_id`、`channel_connections.owner_user_id` | 认证引入前的历史行 | 先 backfill，再切 `NOT NULL` |
| **租约 / 抢占状态** | `runs.owner_worker_id`、`runs.lease_expires_at`、`runs.cancel_action`、`scheduled_tasks.lease_owner`、`scheduled_tasks.lease_expires_at`、`mcp_tasks.lease_owner`、`mcp_tasks.lease_expires_at`、`mcp_tasks.notification_lease_owner`、`mcp_tasks.notification_lease_expires_at`、`subagent_batch_items.lease_owner`、`subagent_batch_items.lease_expires_at` | "未被占用"语义 | **保持 nullable**（`NULL` = 无租约，是业务语义，不是缺失） |
| **时间戳** | `runs.cancel_requested_at`、`scheduled_task_runs.started_at`/`finished_at`、`mcp_tasks.last_polled_at`/`completed_at`、`users.last_seen_at`… | 尚未发生 | 保持 nullable |
| **状态机字段** | `runs.cancel_action`、`scheduled_task_runs.occurrence_seq`、`scheduled_task_runs.launch_accounted`、`mcp_tasks.notification_status`、`mcp_tasks.dispatch_version` | 引入时已存在的行没有值 | 先 backfill 语义默认值，再切 `NOT NULL` |
| **隐式 nullable（无 `nullable=` 但类型是 `Mapped[X \| None]`）** | 所有 `Mapped[str \| None]` / `Mapped[datetime \| None]` | SQLAlchemy 从类型注解推导 | 逐一显式声明，避免依赖推导 |
| **隐式 nullable（无注解也无默认）** | `runs.assistant_id: Mapped[str \| None] = mapped_column(String(128))`（`run/model.py:20`）等 | 同上 | 同上 |

### 4.5 现有索引命名（需重命名）

`persistence/bootstrap.py:215-249` 的 `_BASELINE_INDEX_NAMES` 给出基线索引名，
其它来自各模型 `__table_args__` 与 migration。命名风格混杂：
`ix_*`（SQLAlchemy 自动命名）、`uq_*`、`idx_*`。
按本任务规范需要统一为 `uk_<field>` / `idx_<field>`，详见 §15。

---

## 5. PostgreSQL-specific Dependencies

### 5.1 特性使用矩阵（扫描结论）

| PostgreSQL 特性 | 是否使用 | 位置 |
| --- | --- | --- |
| **JSON / JSONB 列** | ✅ 重度 | 24 个应用列（§4.2）+ LangGraph `checkpoints.checkpoint/metadata`、`store.value` |
| **JSONB 函数** | ✅ | `jsonb_each_text`（checkpointer `SELECT_SQL`）、`jsonb` 运算符 `->`/`->>`（checkpointer DeltaChannel stage-1、`json_compat.py`）、`json_typeof` |
| **ARRAY** | ✅（仅第三方） | `array_agg(...)`、`ANY(%s)`、`unnest(%s::text[])` —— checkpointer `SELECT_SQL` / `SELECT_PENDING_SENDS_SQL`、PostgresStore 批量取 |
| **BYTEA** | ✅（仅第三方） | `checkpoint_blobs.blob BYTEA`、`checkpoint_writes.blob BYTEA NOT NULL`，以及 `bl.channel::bytea`、`cw.task_id::text::bytea` 等强转 |
| **native UUID** | ❌ 未使用 | 用户 ID 显式存 `String(36)`（`user/model.py:41-42` 注释"UUIDs are stored as 36-char strings for cross-backend portability"） |
| **ENUM** | ❌ 未使用 | 所有状态列均为 `String(N)` + 注释枚举值 |
| **SERIAL / Sequence** | ⚠️ 隐式 | `run_events.id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)` → PG 渲染 `SERIAL`。**无显式 `Sequence`** |
| **RETURNING** | ✅ 2 处 | `scheduled_task_runs/sql.py:211`、`app/channels/dedupe_store.py:153` |
| **ON CONFLICT** | ✅ 3 处 | `dedupe_store.py:150`、`user/preferences.py:25`、checkpointer 3 条 UPSERT SQL |
| **CTE** | ✅（仅第三方） | PostgresStore 向量搜索 CTE、checkpointer DeltaChannel 两阶段 SQL |
| **Window Function** | ⚠️ 可能（第三方） | 未在应用代码中出现；`runtime/events/store/db.py` 使用 `func.max` 聚合而非窗口 |
| **Partial Index** | ✅ 2 处 | `uq_runs_thread_active`、`idx_users_oauth_identity` |
| **Expression Index** | ❌ | 无 |
| **GIN / GiST** | ❌（应用侧） | PostgresStore 的 `store_prefix_idx` 使用 `text_pattern_ops` btree；pgvector 索引仅在有 embeddings 时创建，本项目未配置 |
| **Advisory Lock** | ✅ 4 处 | `pg_advisory_lock` / `pg_advisory_unlock`（bootstrap）、`pg_advisory_xact_lock` × 3 |
| **LISTEN / NOTIFY** | ❌ 未使用 | — |
| **SKIP LOCKED** | ✅ | `mcp_tasks/sql.py:230,376,480`、`scheduled_tasks/sql.py:332,530` |
| **FOR UPDATE（含 `read=True`/`FOR SHARE`）** | ✅ 大量 | `run/sql.py:762`、`scheduled_task_runs/sql.py` 多处、`scheduled_tasks/sql.py` 多处、`mcp_tasks/sql.py:169,268,303,339,413,589` |
| **`hashtext()`** | ✅ | `runtime/events/store/db.py:148` |
| **`CREATE INDEX CONCURRENTLY`** | ✅（仅第三方） | checkpointer 3 条、PostgresStore 2 条 |
| **`CREATE EXTENSION`** | ⚠️（仅第三方，未启用） | PostgresStore `VECTOR_MIGRATIONS` |
| **pgvector** | ❌ 未使用 | DeerFlow 未给 `PostgresStore` 传 `index` 配置（`runtime/store/async_provider.py:91-94` 只传 conn_string），因此向量迁移从不执行 |
| **PostgreSQL dialect imports** | ✅ | `sqlalchemy.dialects.postgresql.insert`（`user/preferences.py:4`）；测试中 `from sqlalchemy.dialects import postgresql` 出现于 5 个测试文件 |
| **libpq 专有 DSN 语义** | ✅ | `postgres_schema.py` 全文件的 `options=-c …` 处理 |
| **`SET LOCAL idle_in_transaction_session_timeout`** | ✅ | `bootstrap.py:552` |
| **`make_interval(secs => …)`** | ✅ | `dedupe_store.py:157,174` |
| **`text_pattern_ops`** | ✅（仅第三方） | PostgresStore `store_prefix_idx` |

### 5.2 需要"重点标记"的违规查询（违反 §6 Query 规范）

按本任务"避免 database-specific business logic / 复杂数据库函数"的原则，以下应标记为**违规**：

1. `runtime/events/store/db.py:148` —— `pg_advisory_xact_lock(hashtext(CAST(:thread_id AS text))::bigint)`：
   数据库函数承担了"线程 → 锁 key"的业务映射。
2. `app/channels/dedupe_store.py:145-172` —— 单条 SQL 里同时表达"原子获取 + TTL 回收 + 条件更新 +
   返回值判定"，并依赖 `now()` / `make_interval` / `ON CONFLICT` / `RETURNING` 四个 PG 语义。
3. `persistence/thread_meta/sql.py:230-261` 经 `json_compat.py` 生成的
   `CASE WHEN json_typeof(...) = 'number' AND (col ->> 'k') ~ '^-?[0-9]+$' THEN CAST(... AS BIGINT) END = ?`
   —— deeply nested expression + PG 正则 + 类型系统依赖。
4. `persistence/run/sql.py:207,228-229`、`scheduled_task_runs/sql.py:847` —— JSON 路径作为
   `WHERE` / `SELECT` 表达式。
5. `persistence/scheduled_task_runs/sql.py:203-212` —— `UPDATE … RETURNING` 作为序号分配原语。
6. LangGraph checkpointer / store 的所有 SQL（第三方，不可改，只能替换）。

---

## 6. MySQL 8.0.24 Compatibility

### 6.1 版本能力核对（8.0.24 为准）

| 能力 | MySQL 8.0.24 是否有 | 说明 |
| --- | --- | --- |
| `SELECT … FOR UPDATE` | ✅ | InnoDB 原生 |
| `SELECT … FOR SHARE`（等价 `read=True`） | ✅ | 8.0.1+ |
| `SKIP LOCKED` | ✅ | 8.0.1+ |
| `NOWAIT` | ✅ | 8.0.1+ |
| CTE（`WITH`） | ✅ | 8.0.1+ |
| 窗口函数 | ✅ | 8.0.2+ |
| **`RETURNING`** | ❌ **不支持** | MySQL 至今无此语法（MariaDB 有）。**硬阻塞** |
| **Partial / Filtered Index** | ❌ **不支持** | 需生成列或函数索引 workaround |
| **函数索引（表达式索引）** | ✅ | 8.0.13+，`INDEX ((expr))` |
| **生成列** | ✅ | `GENERATED ALWAYS AS (…) STORED/VIRTUAL` |
| `INSERT … ON DUPLICATE KEY UPDATE` | ✅ | 但**不支持** `DO UPDATE … WHERE <cond>` |
| 行别名 `INSERT … AS new` | ✅ | 8.0.19+，8.0.24 OK |
| `LAST_INSERT_ID(expr)` | ✅ | 可用于序列分配 |
| `GET_LOCK` / `RELEASE_LOCK` | ✅ | **连接级**，非事务级 |
| `json_table` / `json_arrayagg` / `json_extract` | ✅ | 8.0.4+ / 5.7+ —— 但**本项目规范禁止使用 JSON** |
| **Advisory Lock（事务级）** | ❌ **无等价物** | `GET_LOCK` 不随 commit/rollback 释放 |
| `idle_in_transaction_session_timeout` | ❌ 无此变量 | 有 `innodb_lock_wait_timeout`、`wait_timeout`，语义不同 |
| `CREATE INDEX CONCURRENTLY` | ❌ 无此语法 | InnoDB 8.0 默认 online DDL（`ALGORITHM=INPLACE, LOCK=NONE`），但语法不同 |
| `CREATE SCHEMA` + `search_path` | ⚠️ 语义不同 | MySQL 的 schema ≡ database，没有 search_path |
| `BOOLEAN` | ⚠️ 别名 | `BOOL`/`BOOLEAN` = `TINYINT(1)` |
| `DATETIME` 时区 | ❌ | 无时区；`TIMESTAMP` 会做 UTC 换算且上限 2038 |
| `DATETIME` 亚秒精度 | ⚠️ 需显式 | 默认 `DATETIME`(0 位小数)，需 `DATETIME(6)` |
| InnoDB 索引键长上限 | ⚠️ 3072 字节 | utf8mb4 下单列最多 768 字符 |
| `TEXT` 作主键 | ❌ | 主键不能用前缀长度；`TEXT` 必须转 `VARCHAR(N)` |
| `MAX(x) … FOR UPDATE` | ⚠️ 行为不同 | PG 直接报错，MySQL 会锁扫描行 —— 语义可用但锁范围需实测 |

### 6.2 结论

**MySQL 8.0.24 足以承载 Application Data 层的全部查询模式**，前提是：
- 放弃 `RETURNING`，改为 `LAST_INSERT_ID(expr)` 或 `SELECT … FOR UPDATE` + `UPDATE`；
- 放弃 partial index，改用生成列 + 唯一索引；
- 放弃 advisory lock，改用锁表（sentinel row + `SELECT … FOR UPDATE`）或
  重新设计为"唯一约束 + 重试"；
- 把 JSON 列改为 `TEXT` + 应用层序列化（或规范化子表）。

**Checkpoint / Store 层无法通过配置迁移**，因为第三方包的 SQL 无法在 MySQL 上执行。

---

## 7. MySQL Rule Compliance Analysis

### 7.1 数据类型规则

| 规范 | 当前用法 | 合规改造方案 |
| --- | --- | --- |
| 禁止 `JSON` | 24 个应用列 + LangGraph 3 列 | **方案 A（TEXT 序列化）**：`sa.JSON` → `sa.Text` + 应用层 `json.dumps(ensure_ascii=False)`；`persistence/engine.py:25-27` 已有 `_json_serializer` 可复用。**方案 B（规范化）**：见下 |
| 禁止 `BLOB` 系列 | `checkpoint_blobs.blob BYTEA`、`checkpoint_writes.blob BYTEA` | 见 §11 与 §12；**这是最困难的一项** |
| 禁止 `ENUM` | 未使用 | 无需改造（现有 `String(N)` 已合规） |
| 禁止 Geometry / SET | 未使用 | — |
| 禁止 specialized types | `BINARY(16)`（仅第三方 MySQL 包使用） | 第三方包不合规，见 §16 |
| 优先 `VARCHAR`/`TEXT`/`CHAR` | 已普遍使用 `String(N)`/`Text` | 需补长度审计（见 §6.1 键长上限） |
| 优先 `BIGINT`/`INT` | `run_events.id`、各 token 计数 | 合规 |
| 优先 `BOOLEAN` | `users.needs_setup`、`subagent_batch_items.result_truncated`、`mcp_tasks.*` | MySQL 落为 `TINYINT(1)`，语义等价 |
| 优先 `DATETIME` | 全部时间列均为 `DateTime(timezone=True)` | **必须显式改为 `DATETIME(6)`**，否则截断亚秒（见 §13.6） |

**JSON 列的三条落地路线对比**

| 方案 | 适用列 | 优点 | 缺点 |
| --- | --- | --- | --- |
| **A. TEXT 序列化** | 24 列中的绝大多数（配置文档、token 统计、metadata、scopes、spec） | 零语义损失、迁移机械、应用层已有序列化习惯（`engine.py::_json_serializer`、`events/store/db.py::_content_to_db`） | 失去 DB 侧 JSON 查询能力 —— 但**现有代码本来就依赖 `json_compat.py` 这种方言专有 hack**，改成应用层过滤反而更符合 §6 规范 |
| **B. Schema 规范化** | 结构稳定、需要过滤/排序的少量列 | 真正的索引化查询 | 需要逐列设计；`agents.config`、`scheduled_tasks.schedule_spec`、`subagent_batches.execution_spec` 这类"整文档往返"字段**明确不应规范化**（`agents/model.py:1-14` 的注释解释了为什么：新增 `AgentConfig` 字段必须零 Schema 变更地往返） |
| **C. 外部存储** | 大 payload：`mcp_tasks.result_artifact`、`subagent_batch_items.result`、`runs` 的 `first_human_message`/`last_ai_message` | 保持 DB 轻量 | 引入新基础设施依赖 |

**推荐组合**：
- **绝大多数 JSON 列 → 方案 A（`TEXT`）**，其中需要按 key 过滤的
  `threads_meta.metadata_json`（置顶/归档）与 `runs.metadata_json`
  （`regenerate_from_run_id`/`replay_kind`/`scheduled_task_run_id`）
  → **方案 B（拆出少量 scalar 列）**，因为这些过滤路径在
  `thread_meta/sql.py:230,247,261`、`run/sql.py:207,228-229`、
  `scheduled_task_runs/sql.py:847` 都是**热路径**，靠 `TEXT LIKE` 扫描不可接受。
- **大 payload → 方案 C**，见 §11.3。

### 7.2 Table / Column 规范

**表名前缀 `ag_`**

现状：**0 张表带前缀**。需要改名的对象：

| 现名 | 目标名 |
| --- | --- |
| `runs` | `ag_runs` |
| `run_events` | `ag_run_events` |
| `threads_meta` | `ag_threads_meta` |
| `users` | `ag_users` |
| `user_preferences` | `ag_user_preferences` |
| `agents` | `ag_agents` |
| `projects` | `ag_projects` |
| `feedback` | `ag_feedback` |
| `personal_access_tokens` | `ag_personal_access_tokens` |
| `scheduled_tasks` | `ag_scheduled_tasks` |
| `scheduled_task_runs` | `ag_scheduled_task_runs` |
| `mcp_tasks` | `ag_mcp_tasks` |
| `managed_subagents` | `ag_managed_subagents` |
| `subagent_batches` | `ag_subagent_batches` |
| `subagent_batch_items` | `ag_subagent_batch_items` |
| `channel_connections` | `ag_channel_connections` |
| `channel_credentials` | `ag_channel_credentials` |
| `channel_oauth_states` | `ag_channel_oauth_states` |
| `channel_conversations` | `ag_channel_conversations` |
| `webhook_deliveries` | `ag_webhook_deliveries` |
| `alembic_version` | `ag_alembic_version`（需 `version_table` 配置 + `bootstrap.py:361,416` 同步） |
| `checkpoints` | `ag_checkpoint`（或 `ag_checkpoints`，见 §20 待确认） |
| `checkpoint_blobs` | `ag_checkpoint_blob` |
| `checkpoint_writes` | `ag_checkpoint_write` |
| `checkpoint_migrations` | `ag_checkpoint_migration` |
| `store` | `ag_memory` / `ag_store`（见 §20 待确认） |

**必须同步修改的位置**（否则运行期不一致）：
- ORM：`__tablename__`（20 处）
- Migration：新 revision 中的 `op.create_table` / `op.add_column` 的 `table=` 参数
- Raw SQL：`app/channels/dedupe_store.py:150,174`（`webhook_deliveries`）
- Bootstrap 常量：`_BASELINE_TABLE_NAMES`、`_CANONICAL_0019_SCHEMA_FLOOR`
  （`bootstrap.py:134-210`）—— 共 20 张表的表名与列名硬编码
- `_read_database_revision`：`SELECT version_num FROM alembic_version`（`bootstrap.py:361`）
- `_reflect_state`：`"alembic_version" in reflected`（`bootstrap.py:416`）
- Alembic：`version_table` 配置（当前 `env.py` 未设置）
- `_env_filters.LANGGRAPH_OWNED_TABLES`（`_env_filters.py:28-35`）
- Health check / 集成测试：`tests/test_pg_schema_integration.py` 等 8 个 PG 测试文件

**主键**：所有表已有 PK，合规。注意 MySQL 不允许 `TEXT` 列作 PK，因此
`ag_checkpoint*` 的 `thread_id` / `checkpoint_ns` / `checkpoint_id` / `channel` / `version`
**必须从 `TEXT` 收紧为 `VARCHAR(N)`**（见 §6.1 与 §8.3）。

**禁止 FK**：4 处 FK 需要移除（§4.3），并在新 baseline 中不创建。
`channel_credentials` / `channel_conversations` / `user_preferences` /
`subagent_batch_items` 的级联删除需要**移到应用层**（现在依赖
`ON DELETE CASCADE`）。这是一处**功能回归点**，需要显式实现级联删除逻辑。

**Relationship 索引**：4 组 relationship ID 需要显式索引（现在靠 FK 隐式提供的
PG 索引 + 已有 `ix_*`）：

| 关系 | 需要的索引 |
| --- | --- |
| `channel_credentials.connection_id` → `channel_connections.id` | `idx_connection_id`（现为 `ix_channel_credentials_connection_id`? 需核对） |
| `channel_conversations.connection_id` → `channel_connections.id` | `idx_channel_conversations_connection_id`（已存在） |
| `user_preferences.user_id` → `users.id` | `PRIMARY KEY (user_id, key)` leftmost prefix 已覆盖 |
| `subagent_batch_items.batch_id` → `subagent_batches.id` | `idx_batch_id`（现有 `ix_subagent_batch_items_batch_id`? 需核对） |

### 7.3 NOT NULL / DEFAULT

**必须补 backfill 再切 NOT NULL 的列**（无合理默认值，禁止制造虚假默认）：

| 列 | 现状 | Backfill 策略 |
| --- | --- | --- |
| `ag_runs.user_id` | nullable（认证前数据） | 从 `ag_threads_meta.user_id` 按 `thread_id` 关联回填；`scripts/migrate_user_isolation.py` 已有同类逻辑可参考；仍有孤儿行则保留 nullable 并记录 |
| `ag_run_events.user_id` | nullable | 同上（按 `thread_id` 回填） |
| `ag_threads_meta.user_id` | nullable | 无法回填时保留 nullable |
| `ag_feedback.user_id` | nullable | 按 `thread_id` 回填 |
| `ag_channel_connections.owner_user_id` | **已 NOT NULL** | 无需处理 |
| `ag_scheduled_task_runs.occurrence_seq` | nullable（0022 引入） | 对历史行按 `(task_id, created_at, id)` 排序生成序号后回填 |
| `ag_scheduled_task_runs.launch_accounted` | nullable（0022 引入） | 按 `run_id IS NOT NULL` 推导后回填 |
| `ag_mcp_tasks.notification_status` / `dispatch_version` / `dispatch_attempt` | nullable（0013 引入） | 按 `dispatch_event IS NULL` 推导语义默认值后回填 |
| `ag_runs.cancel_action` | nullable | **保持 nullable**（`NULL` = 无取消请求，是业务语义） |
| `ag_runs.operation_kind` | 已 NOT NULL + `server_default 'run'` | 合规 |
| `ag_runs.status` / `multitask_strategy` | 有 Python default 但**无 server_default** | 需补 `server_default` 或明确说明由应用层保证 |
| `ag_runs.message_count` / `total_*_tokens` / `llm_call_count` / `lead_agent_tokens` / `subagent_tokens` / `middleware_tokens` | Python default 0，无 server_default | 补 `DEFAULT 0` |
| `ag_threads_meta.status` | Python default `"idle"`，无 server_default | 补 `DEFAULT 'idle'` |
| `ag_run_events.content` / `event_metadata` | Python default，无 server_default | 补 `DEFAULT ''` / `DEFAULT '{}'`（TEXT 序列化后为字符串） |

**保持 nullable 的列**（`NULL` 有明确业务语义，不得为了满足"全部 NOT NULL"而造默认值）：
所有 lease / owner / 时间戳列（见 §4.4 第 3、4 行）。

### 7.4 中文 COMMENT

现状：**零个 TABLE COMMENT、零个 COLUMN COMMENT**。
MySQL 的 `COMMENT` 是列/表定义的一部分，必须在 `create_table` 时写入，
后续 `ALTER TABLE … COMMENT` 会重建表（8.0 支持 `ALTER TABLE … COMMENT` 为 in-place，
但 `MODIFY COLUMN … COMMENT` 对某些类型是 copy 操作）。

**需要在迁移设计中补齐注释的对象（清单）**

| 表 | 需要中文注释的列（示例，非全量） |
| --- | --- |
| `ag_runs` | `status`（"pending"/"running"/"success"/"error"/"timeout"/"interrupted" 枚举语义）、`operation_kind`、`multitask_strategy`、`owner_worker_id`（多 Worker 租约持有者）、`lease_expires_at`、`cancel_action`、`cancel_requested_at`、`stop_reason`、`token_usage_by_model`（JSON 文档字符串）、`follow_up_to_run_id` |
| `ag_run_events` | `seq`（线程内单调递增）、`category`、`event_type`、`content`（可能是 JSON 字符串）、`event_metadata` |
| `ag_threads_meta` | `incarnation`（线程化身，见 0019）、`status`、`project_id`、`metadata_json`（含 pinned/archived 标志） |
| `ag_scheduled_tasks` / `ag_scheduled_task_runs` | `schedule_type`/`schedule_spec`、`overlap_policy`、`context_mode`、`lease_owner`、`last_occurrence_seq`、`occurrence_seq`、`launch_accounted`、`attempt_count` |
| `ag_mcp_tasks` | `remote_task_id`、`driver_name`、`notification_status`、`dispatch_version`、`event_fingerprint`、`next_poll_at`、`input_required` |
| `ag_subagent_batches` / `ag_subagent_batch_items` | `submission_key`、`execution_spec`、`acceptance_criteria`、`acceptance_verdict`、`token_usage`、`item_key`、`position` |
| `ag_users` / `ag_user_preferences` | `system_role`、`needs_setup`、`token_version`、`oauth_provider`/`oauth_id` |
| `ag_agents` | `config`（`AgentConfig` 文档减 `name`）、`soul` |
| `ag_projects` | `presentation`、`instructions` |
| `ag_channel_*` | `scopes_json`、`capabilities_json`、`encrypted_*`（加密载荷说明） |
| `ag_webhook_deliveries` | `first_seen`（TTL 起点） |
| **LangGraph 表** | `thread_id`、`checkpoint_ns`、`checkpoint_id`、`parent_checkpoint_id`、`channel`、`version`、`type`、`blob`（序列化格式说明）、`task_path` |

### 7.5 Index 规范

见 §15（独立章节）。

### 7.6 Query 规范

§5.2 已列出 6 类违规。迁移到 MySQL 后**不得**用
`JSON_UNQUOTE(JSON_EXTRACT(...))` 或 `REGEXP` 复刻这些查询
（那是把 PG 专有 trick 换成 MySQL 专有 trick，同样违反 §6）。
正确做法是把过滤下沉到应用层，或按 §7.1 方案 B 拆列。

### 7.7 Migration 规范

**当前状态与本任务规范的映射**

| 任务规范 | 本项目现状 | 落地方式 |
| --- | --- | --- |
| 已提交 SQL schema 视为 immutable | 项目**没有** SQL schema 文件；等价物是 Alembic revision `0001_baseline` … `0023_user_preferences`（`persistence/migrations/versions/`） | **把 0001–0023 视为 immutable**，不修改 |
| Docker initialization scripts immutable | 项目**没有** DB init 脚本；Schema 由 `persistence/bootstrap.py:603-701` 的三分支状态机在启动期生成 | 三分支状态机本身是"初始化流程"；需要新增一个 **numbered bootstrap 分支**（如 `_bootstrap_mysql`），而不是修改现有分支 |
| 新 Schema 修改必须创建 `00N_xxx.sql` | 现有约定是 `00NN_<name>.py`（Alembic Python revision） | 创建 `0024_mysql_baseline.py` 起的新 revision；**保持 Python revision 形式**（项目约束），编号连续递增 |
| Fresh Instance：original → 001 → 002 → latest | 等价：`create_all` → `stamp head`（`bootstrap.py:628-632`）或 `stamp 0001_baseline` → `upgrade head`（`bootstrap.py:634-653`） | MySQL 走**新的 empty 分支**：创建 `ag_` 前缀 baseline 表 + 中文注释 + 生成列索引，再 `stamp head` |
| Existing Instance：DBA 逐条执行每个新 migration | 等价：`alembic upgrade head`，由 `bootstrap_schema` 驱动 | 需要为 MySQL 提供显式的 DBA 执行清单（见 §19） |
| 严格按数字顺序执行 | Alembic 自身保证链式顺序 | 保持 |
| 不得修改历史 SQL | 现在已有 `0021_batch_acceptance.py` 使用 `sa.JSON()`、`0013` 使用 `sa.JSON()`、`0002` 使用 `sa.JSON()` | 这些**不改**。MySQL baseline 在新 revision 中重建表结构 |

> **需要用户确认的规范偏差**：任务要求 "`001_xxx.sql` 形式的 numbered SQL migration"，
> 而本项目已确立 "Alembic Python revision" 为唯一 migration 载体，
> 并且 `bootstrap.py` 的整个状态机（`_get_head_revision` / `_KNOWN_REVISIONS` /
> `ScriptDirectory`）都依赖 Alembic 脚本树。若强制改成裸 SQL 文件，
> 需要重写 bootstrap 层。**建议保持 Alembic revision 形式，编号从 0024 续**（见 §20 Q1）。

---

## 8. LangGraph Checkpoint Analysis

### 8.1 版本与实现确认（基于 lockfile 与实际安装）

| 项 | 值 | 证据 |
| --- | --- | --- |
| `langgraph` | **1.2.9** | `packages/harness/pyproject.toml:30` `>=1.2.9,<1.3`；`uv.lock`；`.venv` 实测 |
| `langgraph-checkpoint` | **4.1.1** | `.venv` 实测（`langgraph_checkpoint-4.1.1.dist-info`） |
| `langgraph-checkpoint-postgres` | **3.1.1**（声明） | `packages/harness/pyproject.toml:75` `>=3.1.1,<3.2`；`uv.lock:2244-2255` |
| `langgraph-checkpoint-sqlite` | **3.1.1**（安装） | `packages/harness/pyproject.toml:48`；`.venv` 实测 |
| `langgraph-api` | **0.10.0** | `.venv` 实测 |
| `langchain` / `langchain-core` | 1.3.14 / 1.4.9 | `.venv` 实测 |
| **本地 venv 是否装了 PG checkpointer** | **未安装**（`postgres` extra 未 sync） | `importlib.metadata.version("langgraph-checkpoint-postgres")` → `NOT INSTALLED` |

**当前 Saver**：

| 路径 | Saver |
| --- | --- |
| 异步（Gateway 主路径） | `langgraph.checkpoint.postgres.aio.AsyncPostgresSaver`，连接来自 `psycopg_pool.AsyncConnectionPool`（`runtime/checkpointer/async_provider.py:88-140`） |
| 同步（TUI / CLI / `DeerFlowClient`） | `langgraph.checkpoint.postgres.PostgresSaver`，`from_conn_string`（`runtime/checkpointer/provider.py:132-147`） |
| Delta 模式包装 | `CachedHistorySaver`（`runtime/checkpointer/cached_saver.py`），缓存后端 `MemoryCheckpointHistoryCache` 或 Redis（`runtime/checkpoint_cache/`） |

**sync / async**：两条路径都提供，且共享同一个 `CheckpointerConfig`。

**`checkpoint_channel_mode`**：`full`（默认）或 `delta`。
`delta` 使用 LangGraph `DeltaChannel`（`agents/thread_state.py:15,385-393`），
`messages` 通道由 `merge_message_writes` 折叠。

### 8.2 Checkpoint Schema（3.1.1 实测 DDL）

从 `langgraph_checkpoint_postgres-3.1.1-py3-none-any.whl` 提取的 `MIGRATIONS`：

```sql
CREATE TABLE IF NOT EXISTS checkpoint_migrations (v INTEGER PRIMARY KEY);

CREATE TABLE IF NOT EXISTS checkpoints (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    parent_checkpoint_id TEXT,
    type TEXT,
    checkpoint JSONB NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}',
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);

CREATE TABLE IF NOT EXISTS checkpoint_blobs (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL,
    version TEXT NOT NULL,
    type TEXT NOT NULL,
    blob BYTEA,
    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
);

CREATE TABLE IF NOT EXISTS checkpoint_writes (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    channel TEXT NOT NULL,
    type TEXT,
    blob BYTEA NOT NULL,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);

ALTER TABLE checkpoint_blobs ALTER COLUMN blob DROP not null;
SELECT 1;                                   -- no-op 占位
CREATE INDEX CONCURRENTLY IF NOT EXISTS checkpoints_thread_id_idx      ON checkpoints(thread_id);
CREATE INDEX CONCURRENTLY IF NOT EXISTS checkpoint_blobs_thread_id_idx ON checkpoint_blobs(thread_id);
CREATE INDEX CONCURRENTLY IF NOT EXISTS checkpoint_writes_thread_id_idx ON checkpoint_writes(thread_id);
ALTER TABLE checkpoint_writes ADD COLUMN IF NOT EXISTS task_path TEXT NOT NULL DEFAULT '';
```

**关键 SQL（PostgreSQL 专有）**

```sql
-- SELECT_SQL：一次查询取出 checkpoint + channel_values + pending_writes
select thread_id, checkpoint, checkpoint_ns, checkpoint_id, parent_checkpoint_id, metadata,
  (select array_agg(array[bl.channel::bytea, bl.type::bytea, bl.blob])
   from jsonb_each_text(checkpoint -> 'channel_versions')
   inner join checkpoint_blobs bl
     on bl.thread_id = checkpoints.thread_id
    and bl.checkpoint_ns = checkpoints.checkpoint_ns
    and bl.channel = jsonb_each_text.key
    and bl.version = jsonb_each_text.value) as channel_values,
  (select array_agg(array[cw.task_id::text::bytea, cw.channel::bytea, cw.type::bytea, cw.blob]
                    order by cw.task_id, cw.idx)
   from checkpoint_writes cw where …) as pending_writes
from checkpoints

-- SELECT_PENDING_SENDS_SQL
select checkpoint_id,
       array_agg(array[type::bytea, blob] order by task_path, task_id, idx) as sends
from checkpoint_writes
where thread_id = %s and checkpoint_id = any(%s) and channel = '{TASKS}'
group by checkpoint_id

-- UPSERT_* 使用 ON CONFLICT (…) DO UPDATE / DO NOTHING + %s 占位符

-- DeltaChannel stage-1（动态列生成）
SELECT checkpoint_id, parent_checkpoint_id,
       checkpoint -> 'channel_versions' ->> %s AS ver_0,
       (checkpoint -> 'channel_values' -> %s) IS NOT NULL AS hs_0, …
FROM checkpoints WHERE thread_id = %s AND checkpoint_ns = %s
  AND (%s::text IS NULL OR checkpoint_id < %s)
ORDER BY checkpoint_id DESC LIMIT %s
```

### 8.3 是否存在可用的 MySQL Checkpointer

**必须严格区分（本任务的硬要求）**

| 类别 | 结论 |
| --- | --- |
| **LangGraph 官方支持** | ❌ **不存在**。官方仅提供 `langgraph-checkpoint`（core：base/memory/serde）、`langgraph-checkpoint-sqlite`、`langgraph-checkpoint-postgres`。已实测 `.venv/…/langgraph/checkpoint/` 下只有 `base`、`memory`、`serde`、`sqlite` 四个子包，全仓库对 `mysql` **零命中** |
| **官方独立 package** | ❌ 无 `langgraph-checkpoint-mysql` 官方包 |
| **第三方实现** | ⚠️ 存在：`langgraph-checkpoint-mysql`（PyPI v3.0.0，2026-01-23，作者 Theodore Ni / `tni`，MIT，仓库 `tjni/langgraph-checkpoint-mysql`）。提供 `PyMySQLSaver` / `AIOMySQLSaver` / `AsyncMySaver`，以及 `langgraph/store/mysql` 的对应实现。要求 MySQL ≥ 8.0.19（8.0.24 满足）。**它不是 LangChain/LangGraph 官方组件** |
| **当前项目已有实现** | ❌ 无。项目只做**包装**（`CachedHistorySaver`）与**配置选择**，没有自定义 saver |
| **需要自行实现** | ✅ **这是当前唯一的合规路径**（理由见 §16） |

### 8.4 第三方 MySQL Saver 的实测 DDL（不合规证据）

`langgraph/checkpoint/mysql/base.py` 的 `MIGRATIONS`（节选关键点）：

```sql
CREATE TABLE IF NOT EXISTS checkpoints (
    thread_id VARCHAR(150) NOT NULL,
    checkpoint_ns VARCHAR(150) NOT NULL DEFAULT '',
    checkpoint_id VARCHAR(150) NOT NULL,
    parent_checkpoint_id VARCHAR(150),
    type VARCHAR(150),
    checkpoint JSON NOT NULL,                  -- ❌ 违反"禁止 JSON"
    metadata JSON NOT NULL DEFAULT ('{}'),     -- ❌ 违反"禁止 JSON"
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);

CREATE TABLE IF NOT EXISTS checkpoint_blobs (
    …
    `blob` LONGBLOB,                           -- ❌ 违反"禁止 BLOB/LONGBLOB"
    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
);

CREATE TABLE IF NOT EXISTS checkpoint_writes (
    …
    `blob` LONGBLOB NOT NULL,                  -- ❌ 违反"禁止 BLOB/LONGBLOB"
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);

-- 生成列 + BINARY(16) 哈希
ALTER TABLE checkpoints
  ADD COLUMN checkpoint_ns_hash BINARY(16) AS (UNHEX(MD5(checkpoint_ns))) STORED,  -- ❌ specialized type
  DROP PRIMARY KEY,
  ADD PRIMARY KEY (thread_id, checkpoint_ns_hash, checkpoint_id);
```

并且 SQL 中大量使用 `json_table` / `json_keys` / `json_extract` / `json_unquote` /
`json_arrayagg` / `json_contains` / `to_base64` / `UNHEX(MD5())`。

**判定**：`需要定制 / 不兼容当前 Schema 规范`，**不能**标记为"直接支持"。
表名无 `ag_` 前缀、无中文 COMMENT、无 FK（这一点合规）、
PK 键长设计（`VARCHAR(150)`×3、`VARCHAR(2000)`+哈希）也与我们自己的键长策略不一致。

### 8.5 Checkpoint 迁移难度判定：**Hard**

理由：
- 序列化格式是 **msgpack（主）/ pickle（回退）**，不是 JSON —— 见 §12。
- `checkpoint` / `metadata` 两列是 JSONB，且 `checkpoint` 内部结构
  （`channel_versions`、`channel_values`、`versions_seen`）被 SQL 直接查询。
- 并发写语义：checkpointer 用 `ON CONFLICT DO NOTHING`（blobs）与
  `ON CONFLICT DO UPDATE`（checkpoints/writes），**靠主键冲突吸收并发写**。
  MySQL 的 `INSERT IGNORE` / `ON DUPLICATE KEY UPDATE` 语义相近但
  `INSERT IGNORE` 会**吞掉所有错误**（包括数据截断），需要显式评估。
- `CREATE INDEX CONCURRENTLY` 语法在 MySQL 上直接报错。

---

## 9. LangGraph Store / Memory Analysis

### 9.1 三个"Memory"概念必须分开

本任务把 "LangGraph Store / Memory" 作为一个职责域，但代码里其实有**三个互不相干**的东西：

| # | 名称 | 实现 | 存储 | 与 PostgreSQL 的关系 |
| --- | --- | --- | --- | --- |
| 1 | **LangGraph Store**（`BaseStore`） | `InMemoryStore` / `SqliteStore` / `PostgresStore`（`runtime/store/*`） | 跟 `database:` 走 | ✅ 直接依赖 |
| 2 | **Agent Memory**（长期记忆） | `deermem` / `mem0` / `honcho` / `openviking` / `noop`（`agents/memory/backends/`），`config.yaml:172-216` 实际为 `manager_class: deermem` | **本地文件 + SQLite FTS5**（`deermem/core/retrieval.py:129` `CREATE VIRTUAL TABLE memory_fts USING fts5(...)`） | ❌ **完全无关** |
| 3 | **Thread metadata store** | `ThreadMetaRow`（`persistence/thread_meta/`） + 内存实现 `thread_meta/memory.py` | 跟 `database:` 走 | ✅ 直接依赖（已计入 Application Data） |

### 9.2 LangGraph Store 的实际使用面（很窄）

全仓库对 `make_store` / `get_store` 的**真实消费者**只有：

- `runtime/store/async_provider.py` / `provider.py`（工厂自身）
- `runtime/store/__init__.py`（导出）
- `app/gateway/deps.py`（`app.state.store` 装配）
- `agents/tools/builtins/setup_agent_tool.py:63` `store.get(agent_name, user_id=user_id)`

即：**Store 只承载 `agents` 命名空间下的少量文档**（agent 定义/记忆元数据），
没有 vector search、没有 semantic memory、没有 namespace 树遍历。
`runtime/runs/manager.py` 里的 `self._store.put/get` 是 **`RunStore`（应用仓储）**，
不是 LangGraph Store。

### 9.3 第三方 MySQL Store 的 DDL（不合规证据）

`langgraph/store/mysql/base.py` 的 `MIGRATIONS`：

```sql
CREATE TABLE IF NOT EXISTS store (
    prefix VARCHAR(500) NOT NULL,        -- namespace
    `key` VARCHAR(150) NOT NULL,
    value JSON NOT NULL,                 -- ❌ 违反"禁止 JSON"
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,   -- ⚠️ TIMESTAMP 2038 上限 + 隐式时区换算
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (prefix, `key`)
);
CREATE INDEX store_prefix_idx ON store (prefix) USING btree;
```

搜索/过滤使用 `json_extract(value, concat('$.', %s))`。

### 9.4 Store 迁移难度：**Moderate → Hard**

- 使用面窄 → 迁移影响小（Moderate）。
- 但**没有任何合规实现**：官方无 MySQL Store；第三方用 `JSON` + `TIMESTAMP`。
  自研一个只支持 `get/put/delete/search(prefix)` 的 `ag_store`（`TEXT` value）成本可控
  —— 因为真实用到的能力只有 `get` 与 `put`（Hard → 实际为 **Moderate**，
  前提是自研而非使用第三方）。

### 9.5 Vector Search

- **pgvector 未启用**：`runtime/store/async_provider.py:91-94` 只传 `conn_string`，
  没有传 `index` 配置，因此 `PostgresStore` 的 `VECTOR_MIGRATIONS` 从不执行，
  `CREATE EXTENSION vector` 从未运行。
- 因此 **Store 层不存在 pgvector 迁移问题**。

---

## 10. Queue / Scheduler Analysis

### 10.1 四个调度/队列子系统

| 子系统 | 表 | 代码 | 关键并发原语 |
| --- | --- | --- | --- |
| **Scheduler**（定时任务） | `scheduled_tasks`、`scheduled_task_runs` | `persistence/scheduled_tasks/sql.py`、`persistence/scheduled_task_runs/sql.py`、`deerflow/scheduler/`、`app/scheduler/` | `pg_advisory_xact_lock`（全局预算）、`with_for_update(skip_locked=True)`（due-task claim）、`UPDATE … RETURNING`（occurrence_seq）、partial unique index（每任务一个活跃 occurrence）、`FOR UPDATE` 行锁（task → occurrence 固定顺序） |
| **MCP Tasks**（远程 MCP 任务轮询） | `mcp_tasks` | `persistence/mcp_tasks/sql.py`、`app/mcp_tasks/` | `with_for_update(skip_locked=True)`（poll claim / cancel claim / notification claim）、`with_for_update(read=True)`（`FOR SHARE`，线程 incarnation 归属校验）、lease + `dispatch_version` 乐观栅栏 |
| **Run ownership**（多 Worker 运行所有权） | `runs` | `persistence/run/sql.py`、`runtime/runs/manager.py`、`runtime/runs/worker.py` | `with_for_update()`、`claim_for_takeover`（条件 UPDATE 关闭 lease 续约竞态）、`uq_runs_thread_active` partial unique index、`IntegrityError` 判定 |
| **Inbound dedupe**（跨 Pod webhook 去重） | `webhook_deliveries` | `app/channels/dedupe_store.py` | `INSERT … ON CONFLICT … WHERE … RETURNING` + `now()` + `make_interval` |

### 10.2 迁移到 MySQL 的逐项行为变化

| # | 现语义 | MySQL 8.0.24 | 变化与影响 | 难度 |
| --- | --- | --- | --- | --- |
| 1 | `pg_advisory_xact_lock(key)` 事务级互斥，commit/rollback 自动释放 | **无等价物**。`GET_LOCK(name, t)` 是**连接级**，必须显式 `RELEASE_LOCK`；连接池归还连接时锁不释放 → 可能永久死锁 | 调度器全局并发预算锁必须重设计。可选：(a) 锁表 sentinel row + `SELECT … FOR UPDATE`；(b) 把预算检查改为"唯一约束 + 计数重试"；(c) 用 `GET_LOCK` + 严格的 `try/finally` + `connection.detach()` 隔离 | **Hard** |
| 2 | `pg_advisory_lock(key)` session 级（bootstrap） | 同上，且 `SET LOCAL idle_in_transaction_session_timeout = 0` **在 MySQL 不存在** | bootstrap 跨进程互斥失去"DDL 期间保活"保护；MySQL 侧连接被服务端 `wait_timeout` 杀掉会静默释放 `GET_LOCK`。需要改成"锁表 + 长事务 + 显式心跳"或"由 DBA 串行执行 migration" | **Hard** |
| 3 | `hashtext(thread_id)::bigint` 生成锁 key | MySQL 无 `hashtext`；`CRC32()` 只有 32 位 | 必须在**应用层**用 `sha256` 派生 64-bit key（`channel_connections/sql.py:401-404` 已有这个模式可复用） | Moderate |
| 4 | `UPDATE … SET x = x + 1 … RETURNING x` | **MySQL 无 `RETURNING`** | 改用 `UPDATE … SET last_occurrence_seq = LAST_INSERT_ID(last_occurrence_seq + 1)` 后 `SELECT LAST_INSERT_ID()`；**必须同连接同事务**。注意：`LAST_INSERT_ID` 语义会与 `AUTO_INCREMENT` 混用（`run_events.id` 是 autoincrement），存在**被覆盖的风险**，需显式隔离或用 `SELECT … FOR UPDATE` + `UPDATE` 两步法 | **Hard** |
| 5 | `INSERT … ON CONFLICT … DO UPDATE … WHERE first_seen < TTL RETURNING channel` | MySQL 的 `ON DUPLICATE KEY UPDATE` **不支持 `WHERE` 条件**，且无 `RETURNING` | 需重构为 `SELECT … FOR UPDATE` + 分支 + `INSERT`/`UPDATE`；或用 `ON DUPLICATE KEY UPDATE first_seen = IF(first_seen < :cutoff, :now, first_seen)` + `ROW_COUNT()`（0/1/2 语义）—— 但 `dedupe_store.py:164-166` 的注释明确说"不依赖 rowcount"，因为跨驱动不可靠。这是**语义降级** | **Hard** |
| 6 | `make_interval(secs => :ttl)` | MySQL 无此函数 | 用 `TIMESTAMPADD(SECOND, -:ttl, NOW())`（接受参数）或 `DATE_SUB(NOW(), INTERVAL :ttl SECOND)` | Easy |
| 7 | `now()` | MySQL 有 `NOW()`，但它是**语句时间**而非事务开始时间 | `created_at <= created_before` 这类比较的边界在长事务里会有细微差别 | Easy（需注意） |
| 8 | `uq_runs_thread_active` partial unique index | **MySQL 无 partial index** | 用生成列 workaround（见 §15.3）：<br>`active_thread_id VARCHAR(64) GENERATED ALWAYS AS (IF(status IN ('pending','running'), thread_id, NULL)) STORED` + `UNIQUE KEY uk_runs_thread_active (active_thread_id)`。MySQL 唯一索引允许多个 `NULL`，语义**精确等价** | Moderate |
| 9 | `idx_users_oauth_identity` partial unique | 同上 | 同 workaround。**额外风险**：`app/gateway/auth/repositories/sqlite.py:36-39` 依赖驱动错误里的约束名判定 OAuth 冲突；MySQL 1062 错误也带 key 名，但**错误对象形状不同**（`exc.orig` 是 `pymysql.err.IntegrityError` 而非 asyncpg 异常），判定逻辑必须改写 | Moderate |
| 10 | `with_for_update(skip_locked=True)` | ✅ MySQL 8.0.1+ 支持 | 语义一致。**但**：无索引时会退化为全表扫描 + 锁全表；且 MySQL 的 `SKIP LOCKED` 与 `FOR SHARE` 组合有额外限制。需核对每个 claim 语句都有可用索引 | Moderate |
| 11 | `with_for_update(read=True)`（`FOR SHARE`） | ✅ MySQL 8.0.1+ `FOR SHARE` | 语义相近。注意 MySQL `FOR SHARE` 的锁类型（共享记录锁）与 PG 的 `FOR KEY SHARE`/`FOR SHARE` 强度不同，`mcp_tasks/sql.py:166-168` 的注释明确依赖"FOR SHARE 会与 FOR NO KEY UPDATE 冲突"这一 PG 特定行为 —— **该假设在 MySQL 上不成立** | Moderate |
| 12 | 默认隔离级别 READ COMMITTED | MySQL/InnoDB 默认 **REPEATABLE READ** | **最大的隐性风险**：RR 下 `SELECT … FOR UPDATE` 与 `INSERT … ON DUPLICATE KEY UPDATE` 会加 **gap lock**，显著提高死锁概率。代码里有明确的锁顺序纪律（`scheduled_task_runs/sql.py:754-757`：task → scheduled-run；`scheduled_tasks/sql.py:680-684`），RR 下需要重新验证；并且"读到的旧快照"会让 `_lock_task` 之后的重复检查行为变化 | **Hard** |
| 13 | `SELECT max(seq) … FOR UPDATE` 在 PG 报错，因此用 advisory lock | MySQL **允许**，会锁扫描行 | 可以退回纯 `FOR UPDATE`，但锁范围（该 thread 的所有 event 行 + gap）需要实测；`uq_events_thread_seq(thread_id, seq)` 索引可用 | Moderate |
| 14 | `INSERT IGNORE` vs `ON CONFLICT DO NOTHING` | MySQL `INSERT IGNORE` 吞掉**所有**错误（含截断/类型错误） | checkpointer 的 blobs upsert 若用 `INSERT IGNORE`，数据截断会静默丢失。需改用 `INSERT … ON DUPLICATE KEY UPDATE col = col`（no-op 更新）来只吸收唯一键冲突 | Moderate |

### 10.3 两个必须显式验证的场景（本任务要求）

**场景 A：同一 `thread_id` 的两个 Agent Run 并发写 Checkpoint**

- PG 行为：`checkpoints` 主键 `(thread_id, checkpoint_ns, checkpoint_id)` 冲突 → `ON CONFLICT DO UPDATE`；
  `checkpoint_blobs` 冲突 → `DO NOTHING`。并发写**不报错**，后写覆盖。
- MySQL 行为：`ON DUPLICATE KEY UPDATE` 语义相近。
  **但**：RR 隔离下两个事务同时对同一 PK 做 upsert，会先取锁 → 后到者等待；
  若两者还持有其它行锁，可能死锁（MySQL 会**主动 kill 一个事务**，PG 同样会，但
  MySQL 的死锁检测粒度/超时（`innodb_lock_wait_timeout` 默认 50s）与 PG 不同）。
- **额外风险**：`runs` 表的 `uq_runs_thread_active`（应用层已保证每线程一个活跃 run）
  在 MySQL 上通过生成列唯一索引实现后，**错误类型不同**（1062 vs 23505），
  `runtime/runs/store/base.py` 的 `IntegrityError` 判定路径需要复核。
- 结论：**需要专门的并发回归测试**（现有 `tests/test_multi_worker_run_ownership.py`、
  `tests/test_multi_worker_postgres_gate.py` 是针对 PG 写的）。

**场景 B：两个 Worker 抢同一队列行（Scheduler / MCP Tasks）**

- PG：`SELECT … FOR UPDATE SKIP LOCKED` → 后到者跳过被锁行，取下一行。
- MySQL：`SKIP LOCKED` 支持，语义一致。
  **但**：RR 隔离下未命中的行会加 gap lock，导致"跳过"行为在间隙上不完全等价；
  并且 `innodb_lock_wait_timeout` 与 PG 的 `lock_timeout` 默认值不同。
- 结论：**需要并发压测**（`scripts/benchmark/concurrency/worker.py` 可作为起点）。

---

## 11. Knowledge / Vector Analysis

### 11.1 现状：项目**不使用** pgvector

| 检查项 | 结论 |
| --- | --- |
| `pgvector` / `vector` 扩展 | ❌ 未使用。`langgraph/store/postgres/base.py` 的 `VECTOR_MIGRATIONS` 只在传入 embeddings 配置时执行，DeerFlow 未传 |
| PostgreSQL 全文检索（`tsvector`/`to_tsquery`） | ❌ 未使用 |
| Embeddings / RAG 存储 | ❌ **不在数据库里**。RAG 通过 `deerflow/community/ragflow/` 以 **HTTP 调用外部 RAGFlow 服务**（`ragflow/tools.py:161-174` 读取 dataset 的 `embedding_model` 元数据） |
| 本地检索 | ✅ **SQLite FTS5**（`deermem/core/retrieval.py:129` `CREATE VIRTUAL TABLE memory_fts USING fts5(...)`），BM25 排序（`retrieval.py:31`），文件级存储（`storage_class: file`，`config.yaml:180`） |
| 任务连续性归档检索 | ✅ SQLite FTS5（`agents/task_continuity/archive.py:120`），且 `config.yaml:152` 默认 `task_continuity.enabled: false` |
| 大 payload 卸载 | ✅ 已存在：`tool_output.externalize_min_chars: 12000` → 写入 `.tool-results` 文件（`config.yaml:71-85`） |

### 11.2 结论：**不需要 MySQL 承担 Vector 职责**

**应当拆分**，而不是把 pgvector 强行换成 MySQL：

```
Agent Runtime
    ├── MySQL 8.0.24          ← Application Data / Checkpoint / Memory Metadata / Operational
    ├── Object Storage        ← Artifacts / Large Payload（已有 .tool-results 雏形）
    ├── Knowledge Service     ← Vector / RAG（当前已是外部 RAGFlow HTTP）
    └── Remote Sandbox        ← 临时工作区
```

**重要**：这个拆分**不是迁移目标，而是现状的准确描述**。当前项目已经满足这个边界
（除了 Checkpoint 依赖 PG）。因此"Knowledge / Vector"在本次迁移中**难度为 Replace
（即：保持现状，不迁移）**，唯一动作是确认 `PostgresStore` 的向量迁移永远不被触发
（现状已满足）。

### 11.3 应该迁到 Object Storage 的数据（回答 §20 问题 11）

| 数据 | 现状 | 建议 |
| --- | --- | --- |
| Tool 结果大文本 | ✅ 已卸载到 `.tool-results` 文件（阈值 12000 字符） | 保持；考虑迁到对象存储以支持多副本 |
| `mcp_tasks.result_artifact`（JSON） | DB 列，`max_result_bytes: 65536`（`config.yaml:256`） | 超过阈值时卸载，DB 只留引用 |
| `subagent_batch_items.result` / `result_preview` | `max_result_chars: 100000`（`config.yaml:135`） | `result` 大文本可卸载 |
| `runs.first_human_message` / `last_ai_message` | DB `TEXT` | 保留（列表页需要，且已截断语义） |
| **Checkpoint 大 blob** | `checkpoint_blobs.blob` / `checkpoint_writes.blob` | **这是最需要卸载的**：`messages` 通道是累积 reducer（`thread_state.py:280-393`），`full` 模式下 blob 随会话长度单调增长。见 §12.4 |
| Uploads / 附件 | `uploads.max_file_size: 52428800`（50MB，`config.yaml:108-110`） | 对象存储 |
| Agent workspace 变更 | `workspace_changes/` | 对象存储 |

---

## 12. Serialization Analysis

### 12.1 序列化格式实测

`langgraph-checkpoint` 4.1.1 的 `langgraph/checkpoint/serde/jsonplus.py:258-288`：

```python
def dumps_typed(self, obj: Any) -> tuple[str, bytes]:
    if obj is None:          return "null", EMPTY_BYTES
    elif isinstance(obj, bytes):     return "bytes", obj
    elif isinstance(obj, bytearray): return "bytearray", obj
    else:
        try:
            return "msgpack", _msgpack_enc(obj)      # ← 主路径（ormsgpack）
        except ormsgpack.MsgpackEncodeError as exc:
            if self.pickle_fallback:
                return "pickle", pickle.dumps(obj)   # ← 回退路径
            raise exc
```

**结论**：
- 默认序列化是 **msgpack（二进制）**，不是 JSON。
- 存在 **pickle 回退**（`pickle_fallback` 默认开启）—— 意味着 blob 里可能出现
  **任意 Python 对象**，且反序列化会**执行代码**（安全面已在别处讨论，此处只关注存储格式）。
- 类型标签（`"null"`/`"bytes"`/`"bytearray"`/`"json"`/`"msgpack"`/`"pickle"`）
  存在 `checkpoint_blobs.type` / `checkpoint_writes.type` 列里。

### 12.2 应用侧序列化现状

| 位置 | 方式 |
| --- | --- |
| `persistence/engine.py:25-27` | `json.dumps(obj, ensure_ascii=False)`（`json_serializer`） |
| `runtime/events/store/db.py:80-89` | `_content_to_db`：非字符串内容 `json.dumps(default=str, ensure_ascii=False)` + metadata 标记 `content_is_json` |
| `runtime/events/store/db.py:57-67` | 读回时按标记 `json.loads` |
| `persistence/agents/file.py` / `managed_subagents/file.py` | 文件型存储（JSON 文档） |
| `agents/memory/backends/deermem/` | 文件（`memory.json` manifest）+ SQLite FTS5 |

即：**应用层已经习惯"JSON 文本 + 显式类型标记"的序列化模式**。
这使得 §7.1 的方案 A（JSON → TEXT）在应用侧是**低摩擦**的。

### 12.3 与"禁止 JSON/BLOB"的冲突面

| 冲突 | 位置 | 严重度 |
| --- | --- | --- |
| **JSON 列** | 24 个应用列 + `checkpoints.checkpoint`/`metadata` + `store.value` | 应用侧可解（TEXT）；LangGraph 侧不可解 |
| **BYTEA / BLOB 列** | `checkpoint_blobs.blob`、`checkpoint_writes.blob` | **不可解**（除非自研 saver） |
| **serialized Python object / msgpack / pickle** | JsonPlusSerializer | **不可解**（除非自研 saver 或改 serde） |
| **binary serialization** | 同上 | 同上 |

### 12.4 四个方案的可行性分析（本任务要求逐一分析）

#### 方案 A：TEXT Serialization

| 维度 | 分析 |
| --- | --- |
| **serialization format** | 应用侧：`json.dumps(ensure_ascii=False)`（沿用 `engine.py::_json_serializer`）。Checkpoint 侧：需要把 msgpack/pickle 改为 **确定性 JSON 文本**（或用 base64 包裹 msgpack） |
| **size** | **关键问题**：`TEXT` 上限 **65,535 字节**。`messages` 通道是累积 reducer，`full` 模式下单个 blob 轻易超过 64KB。若改用 `MEDIUMTEXT`（16MB）/`LONGTEXT`（4GB），则**超出本任务允许的类型清单**（规范只列了 `VARCHAR`/`TEXT`/`CHAR`）。<br>若改用 base64 包裹二进制，体积 **+33%** |
| **performance** | 应用侧 JSON 序列化成本与 PG JSONB 的二进制解析相当；TEXT 的比较/排序基于 collation，比 JSONB 弱，但应用侧不需要 DB 侧 JSON 查询 |
| **compatibility** | 与现有 `_content_to_db` / `_json_serializer` 模式完全兼容；与 `json_compat.py` 的方言 hack 冲突（应一并删除，改为应用层过滤） |
| **migration** | 机械：`sa.JSON` → `sa.Text`；读回时 `json.loads`。需逐列确认"是否有代码依赖 DB 侧 JSON 过滤" |
| **query requirements** | **两处必须拆列而非 TEXT**：`threads_meta.metadata_json`（pinned/archived 过滤，`thread_meta/sql.py:230,247,261`）、`runs.metadata_json`（三个 key 的等值过滤） |
| **结论** | **适用 22/24 个应用列**。Checkpoint 侧**不适用**（体积超 TEXT 上限） |

#### 方案 B：Schema Normalization

| 维度 | 分析 |
| --- | --- |
| 适用 | 只适用于"结构稳定 + 需要过滤/排序"的少量列 |
| 反例（**明确不应规范化**） | `agents.config`（`agents/model.py:1-14` 的注释：新增 `AgentConfig` 字段必须零 Schema 变更往返）、`scheduled_tasks.schedule_spec`、`subagent_batches.execution_spec`、`mcp_tasks.result_artifact`、`projects.presentation` |
| 正例 | `threads_meta.metadata_json` → 拆 `pinned TINYINT(1) NOT NULL DEFAULT 0`、`archived TINYINT(1) NOT NULL DEFAULT 0` + 保留 `metadata_json` TEXT 承载其余字段；`runs.metadata_json` → 拆 `regenerate_from_run_id VARCHAR(64) NULL`、`replay_kind VARCHAR(32) NULL`、`scheduled_task_run_id VARCHAR(64) NULL`（**这三个恰好是唯一被过滤的 key**） |
| **结论** | 用于 **2 张表 / 5 个 scalar 列**，其余走方案 A |

#### 方案 C：External Storage

| 维度 | 分析 |
| --- | --- |
| 适用 | `checkpoint_blobs.blob` / `checkpoint_writes.blob`（大且不需要 DB 侧查询）、`mcp_tasks.result_artifact`、`subagent_batch_items.result` |
| DB 侧保留 | `(thread_id, checkpoint_ns, channel, version, type, blob_ref, blob_size, blob_sha256)` |
| 优点 | 彻底绕开 TEXT 上限；blob 不进 DB，checkpoint 表保持轻量；与已有 `.tool-results` 卸载模式一致 |
| 缺点 | 引入对象存储依赖；**checkpoint 写入路径从 1 次 DB 写变成 "对象存储写 + DB 写"**，需要处理部分失败（对象已写但 DB 未提交 → 孤儿对象，需 GC）；`get_tuple` 路径变成 "DB 读 + N 次对象读"，延迟上升 |
| **结论** | **对 checkpoint blob 是唯一能满足"禁止 BLOB 且不超 TEXT 上限"的方案**，但代价是重写 saver 的读写路径 + 引入 GC |

#### 方案 D：Replace Component

| 维度 | 分析 |
| --- | --- |
| 触发条件 | 第三方 MySQL Saver 强制依赖 JSON/BLOB，违反规范 |
| 实测结论 | ✅ 触发：`langgraph-checkpoint-mysql` 3.0.0 使用 `JSON` + `LONGBLOB` + `BINARY(16)`（§8.4） |
| 三个子选项 | **(D1) fork / customize**：fork `tjni/langgraph-checkpoint-mysql`，把 `JSON`→`TEXT`、`LONGBLOB`→`TEXT`/外部存储、加 `ag_` 前缀与中文注释。**但**：该包的 SQL 大量依赖 `json_table`/`json_keys`/`json_extract`（用于从 `checkpoints.checkpoint` 里提取 `channel_versions`），改成 TEXT 后这些 SQL **全部要重写** —— 工作量接近自研。<br>**(D2) implement adapter**：自己实现一个 `BaseCheckpointSaver` 子类，直接针对目标 Schema 写 SQL。**推荐**。<br>**(D3) replace persistence component**：把 checkpoint 持久化从 LangGraph saver 抽象里拿出来，改为"应用侧 checkpoint 服务"。**不推荐**（会破坏 LangGraph 的 durable execution 契约） |
| **结论** | **D2（自研 saver）**：以 `BaseCheckpointSaver` 为接口，针对 `ag_checkpoint*` 表写 MySQL 方言 SQL；serde 保持 `JsonPlusSerializer` 但**存 TEXT（base64 或确定性 JSON）**；大 blob 走方案 C |

### 12.5 序列化改造的推荐组合

```
checkpoints.checkpoint        → TEXT   （确定性 JSON，结构由应用层保证）
checkpoints.metadata          → TEXT
checkpoint_blobs.blob         → 方案 C（对象存储）+ 引用列；小 blob 内联 TEXT
checkpoint_writes.blob        → 方案 C 同上
store.value                   → TEXT
应用侧 22 列                   → TEXT（json.dumps ensure_ascii=False）
应用侧 2 列（threads_meta/runs 的 metadata）→ 方案 B 拆列
```

---

## 13. Transaction & Concurrency Analysis

### 13.1 隔离级别

| 项 | PostgreSQL | MySQL 8.0.24 / InnoDB | 影响 |
| --- | --- | --- | --- |
| 默认隔离级别 | READ COMMITTED | **REPEATABLE READ** | 快照语义变化；gap lock 引入 |
| 显式设置 | 无（代码未设置 `isolation_level`） | 无 | 需要在新实现里显式 `SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED`，否则所有并发假设需重新证明 |
| 唯一约束冲突 | SQLSTATE `23505` | 错误码 `1062` | 错误判定路径必须改写（`app/gateway/auth/repositories/sqlite.py:36-39` 依赖 asyncpg 异常形状） |
| 死锁检测 | 立即检测 + 报错 | `innodb_deadlock_detect` 默认 ON | 行为相近；但 `innodb_lock_wait_timeout` 默认 **50 秒**，与 PG 的 `lock_timeout`（默认无限）不同 → 等待语义变化 |
| 语句超时 | `command_timeout=30`（`database_config.py:194-198`）通过 asyncpg `connect_args` 传入 | MySQL 需要 `max_execution_time`（SELECT）/ `innodb_lock_wait_timeout`，驱动侧参数名不同 | `_postgres_engine_kwargs` 整个函数需要重写 |

### 13.2 Row Lock / `FOR UPDATE`

- ✅ MySQL 支持 `FOR UPDATE`、`FOR SHARE`、`SKIP LOCKED`、`NOWAIT`。
- ⚠️ **无索引 = 锁全表**：MySQL 在无法使用索引时会锁定扫描到的**所有行**。
  必须逐一核对每个 `with_for_update` 语句都有可用索引
  （`mcp_tasks` 的 `next_poll_at` / `next_cancel_at` / `next_notification_at`、
  `scheduled_tasks` 的 `next_run_at`、`runs` 的 `lease_expires_at`）。
  现有索引：`ix_runs_lease(lease_expires_at)` 存在；`mcp_tasks` / `scheduled_tasks`
  的 `next_*` 索引需核对（见 §15.4）。
- ⚠️ **RR 下的 gap lock**：`SELECT … FOR UPDATE` 在 RR 下对范围扫描加间隙锁，
  这会**改变并发插入行为**。`scheduled_task_runs/sql.py` 的
  `claim_queued_run` 依赖 `WHERE id = ? AND status = 'queued' AND ~older_same_thread`
  的条件 UPDATE 返回 `rowcount == 1` 判定抢占成功；RR 下该语句的锁行为与 RC 不同。
- ⚠️ **`mcp_tasks/sql.py:166-168` 的 PG 专有假设**：注释明确说
  "`FOR SHARE` 会与老写入方的 `FOR NO KEY UPDATE` 冲突，而 `FOR KEY SHARE` 不会"。
  这是 **PostgreSQL 行锁强度模型**（KEY SHARE / SHARE / NO KEY UPDATE / UPDATE）独有的区分，
  **MySQL 没有这个四档模型**（只有 S 锁 / X 锁 + 间隙锁）。该注释所依赖的正确性论证
  在 MySQL 上**不成立**，需要重新设计归属校验。

### 13.3 SKIP LOCKED

- ✅ 8.0.1+ 可用，`mcp_tasks/sql.py:230,376,480`、`scheduled_tasks/sql.py:332,530` 可直接映射。
- ⚠️ 约束：`SKIP LOCKED` 不能与 `NOWAIT` 同用；与 `FOR SHARE` 组合在某些 8.0.x 版本有额外限制；
  在 `UNION` / 聚合子查询中不可用（现有语句都是单表 `SELECT`，OK）。
- ⚠️ 无索引时 `SKIP LOCKED` 仍会扫全表（只是跳过被锁行）→ 性能退化而非错误。

### 13.4 UPSERT / Unique Conflict

| 用法 | PG | MySQL | 备注 |
| --- | --- | --- | --- |
| `ON CONFLICT DO NOTHING` | ✅ | `INSERT IGNORE`（吞所有错误）或 `ON DUPLICATE KEY UPDATE col = col` | **推荐后者**：只吸收唯一键冲突 |
| `ON CONFLICT DO UPDATE` | ✅ | `ON DUPLICATE KEY UPDATE` | 语义相近；`VALUES()` 已废弃（8.0.20+ 警告），应用行别名 `AS new`（8.0.19+） |
| `ON CONFLICT DO UPDATE … WHERE <cond>` | ✅ | ❌ **不支持** | `dedupe_store.py` 必须重构（§10.2 #5） |
| `RETURNING` | ✅ | ❌ **不支持** | 2 处必须重构（§10.2 #4、#5） |

### 13.5 并发 Checkpoint 写入（场景 A）

**现有保护**：`uq_runs_thread_active` partial unique index 保证"每线程至多一个活跃 run"
（`run/model.py:56-70`），因此 checkpoint 并发写的**正常路径是串行的**。

**MySQL 变化**：
- partial unique index → 生成列唯一索引后，**唯一性语义等价**，但错误码从 `23505` 变 `1062`。
- 真正并发写同一 `thread_id` 的 checkpoint（例如 takeover 后的双写）：
  - PG：`ON CONFLICT DO UPDATE` 后写覆盖前写，无异常。
  - MySQL：`ON DUPLICATE KEY UPDATE` 同样覆盖，但**如果两事务还持有其它行的锁，RR 下更易死锁**。
  - MySQL 死锁会 kill 一个事务并抛 `1213`；PG 抛 `40P01`。上层重试策略需要识别新错误码。

### 13.6 时间语义（一个容易漏掉的破坏点）

| 问题 | 证据 | 后果 |
| --- | --- | --- |
| MySQL `DATETIME` 默认精度为 0 | SQLAlchemy 的 MySQL 方言对 `DateTime(timezone=True)` 渲染 `DATETIME`（无 `fsp`） | **亚秒被截断**。`scheduled_task_runs` 的 FIFO 排序是 `order_by(created_at.asc(), id.asc())`，`expire_queued_runs` 用 Python 计算带微秒的 `created_before` 比较 → 截断后**排序与过期判定都会错** |
| `DateTime(timezone=True)` 在 MySQL 上**静默丢弃时区** | MySQL 无时区感知的 `DATETIME` | 只要全部写入都是 `datetime.now(UTC)`（offset 0），wall clock 恰好等于 UTC，**"碰巧正确"**。一旦有任何非 UTC 写入即静默错误 |
| 读回是 naive datetime | SQLite 已有此问题（`events/store/db.py:52-55` 注释） | 代码里已有 `coerce_iso` 与 `_lease_is_alive` 的 `replace(tzinfo=UTC)` 兜底，但**不是所有路径都覆盖**，需要系统审计 |
| `TIMESTAMP` 不可用 | 2038 上限 + 隐式时区换算 | 必须用 `DATETIME(6)`，不能用 `TIMESTAMP` |

**建议**：所有时间列显式改为 `mysql.DATETIME(fsp=6)`，并在应用侧统一
"UTC aware → naive UTC" 的写入边界转换。

---

## 14. Schema Compatibility Matrix

目标表名统一使用 `ag_` 前缀。

| Current Table | Purpose | PostgreSQL-specific Features | Target MySQL Table | Schema Change | Code Change | Risk |
| --- | --- | --- | --- | --- | --- | --- |
| `runs` | Run 元数据、token 统计、多 Worker 租约 | `JSON`×3、partial unique index `uq_runs_thread_active`、`SERIAL`（`run_events` 才是）、`metadata_json ->> 'x'` 过滤 | `ag_runs` | 3×JSON→TEXT（其中 `metadata_json` 拆 3 个 scalar 列）、partial unique → 生成列唯一索引、时间列 `DATETIME(6)`、补 `DEFAULT`、补中文注释 | `persistence/run/model.py`、`persistence/run/sql.py:207,228-229`、`runtime/runs/*`、`runtime/runs/store/base.py` 的 `IntegrityError` 判定 | **High** |
| `run_events` | 运行事件流（含 seq 分配） | `JSON`（`event_metadata`）、`autoincrement` PK、`uq_events_thread_seq` | `ag_run_events` | `event_metadata`→TEXT、`id` 保持 `BIGINT AUTO_INCREMENT`、`content` `TEXT`（不可作索引，现状即如此） | `runtime/events/store/db.py:134-153` 的 advisory lock 路径必须替换（MySQL 会走 `FOR UPDATE` 分支，需实测） | **High** |
| `threads_meta` | 线程元数据（含 pinned/archived） | `JSON`（`metadata_json`）+ **JSON key 过滤**（`json_compat.py`） | `ag_threads_meta` | `metadata_json` 拆 `pinned`/`archived` + 保留 TEXT；删掉 `JsonMatch` 依赖 | `persistence/thread_meta/sql.py:230,247,261`、`persistence/json_compat.py`（可整体废弃） | **Moderate** |
| `users` | 账号 + OAuth 关联 | partial unique index `idx_users_oauth_identity` | `ag_users` | partial unique → 生成列唯一索引；`id` 保持 `VARCHAR(36)` | `app/gateway/auth/repositories/sqlite.py:36-39` 的约束名/错误码判定 | **Moderate** |
| `user_preferences` | 逐 key 偏好（独立 patch） | `JSON`（`value`）、**FK**（`→users.id` CASCADE）、`pg_insert.on_conflict_do_update` | `ag_user_preferences` | `value`→TEXT、**删 FK**（级联删除移到应用层）、upsert 改 MySQL 方言 | `persistence/user/model.py:37`、`persistence/user/preferences.py:4,21-25`、级联删除需新实现 | **Moderate** |
| `agents` | 自定义 Agent 定义（整文档） | `JSON`（`config`） | `ag_agents` | `config`→TEXT（**不规范化**，见 §12.4 方案 B 反例） | 读写路径 JSON 编解码 | **Easy** |
| `projects` | 项目分组 | `JSON`（`presentation`） | `ag_projects` | `presentation`→TEXT | 编解码 | **Easy** |
| `feedback` | 用户反馈 | 无（纯 scalar） | `ag_feedback` | 时间列精度、补中文注释 | 无 | **Easy** |
| `personal_access_tokens` | PAT（scopes 为 JSON 数组） | `JSON`（`scopes`） | `ag_personal_access_tokens` | `scopes`→TEXT（或逗号分隔 `VARCHAR`，见 §20 Q4） | 编解码 | **Easy** |
| `scheduled_tasks` | 定时任务定义 | `JSON`（`schedule_spec`）、`SKIP LOCKED`、`FOR UPDATE`、advisory lock（兄弟表） | `ag_scheduled_tasks` | `schedule_spec`→TEXT、`last_occurrence_seq` 保持 `BIGINT NOT NULL DEFAULT 0`、时间列精度 | `persistence/scheduled_tasks/sql.py:85-87,180,225,332,530,684` 的 SQLite 分支与 MySQL 分支需要新增 | **High** |
| `scheduled_task_runs` | 调度 occurrence（队列） | `UPDATE … RETURNING`、`pg_advisory_xact_lock`、`FOR UPDATE`、partial unique（每任务一个活跃 occurrence，见 0007） | `ag_scheduled_task_runs` | `occurrence_seq`/`launch_accounted` backfill 后 `NOT NULL`、`UPDATE…RETURNING` → `LAST_INSERT_ID(expr)` 或两步法 | `persistence/scheduled_task_runs/sql.py:203-212,315-319,754-763` | **High** |
| `mcp_tasks` | 远程 MCP 任务轮询 | `JSON`×5、`SKIP LOCKED`、`FOR SHARE`（PG 行锁强度假设）、`lease`+`dispatch_version` 栅栏 | `ag_mcp_tasks` | 5×JSON→TEXT、`FOR SHARE` 归属校验需重设计、时间列精度 | `persistence/mcp_tasks/sql.py:161-169,230,268,303,339,376,413,480,589` | **High** |
| `managed_subagents` | 托管子 Agent 定义 | `JSON`（`definition`） | `ag_managed_subagents` | `definition`→TEXT | 编解码 | **Easy** |
| `subagent_batches` | 批量子 Agent 提交 | `JSON`（`execution_spec`）、**FK** | `ag_subagent_batches` | `execution_spec`→TEXT、删 FK | `persistence/subagent_batches/model.py:44` | **Moderate** |
| `subagent_batch_items` | 批量条目（含 lease） | `JSON`×3、**FK**、`SKIP LOCKED`（兄弟表） | `ag_subagent_batch_items` | 3×JSON→TEXT、删 FK（级联删除移到应用层）、backfill `result_truncated` | 同上 + 级联删除 | **Moderate** |
| `channel_connections` | IM 渠道连接 | `JSON`×3、**FK**（被引用）、`pg_advisory_xact_lock`（兄弟表） | `ag_channel_connections` | 3×JSON→TEXT、advisory lock 替换 | `persistence/channel_connections/model.py`、`persistence/channel_connections/sql.py:397-404` | **Moderate** |
| `channel_credentials` | 渠道凭据（加密载荷） | **FK**（CASCADE） | `ag_channel_credentials` | 删 FK + 应用层级联删除 | `persistence/channel_connections/model.py:71` | **Moderate** |
| `channel_oauth_states` | OAuth 状态机 | `JSON`×2 | `ag_channel_oauth_states` | 2×JSON→TEXT | 编解码 | **Easy** |
| `channel_conversations` | 外部会话 → 线程映射 | **FK**（CASCADE） | `ag_channel_conversations` | 删 FK + 应用层级联删除 | `persistence/channel_connections/model.py:106` | **Moderate** |
| `webhook_deliveries` | 跨 Pod 入站去重 | **`ON CONFLICT … WHERE … RETURNING`**、`now()`、`make_interval` | `ag_webhook_deliveries` | 无 JSON/BLOB 问题；**但整个 upsert 语义必须重构** | `app/channels/dedupe_store.py:120-180` | **High** |
| `alembic_version` | Alembic 版本行 | 无 | `ag_alembic_version` | 需配置 `version_table` | `persistence/bootstrap.py:361,416`、`migrations/env.py` | **Moderate** |
| `checkpoints` | LangGraph checkpoint 头 | **`JSONB`×2**、`TEXT` 主键、`CREATE INDEX CONCURRENTLY`、`jsonb_each_text`、`%s` 占位符 | `ag_checkpoint` | 全表重建：`checkpoint`/`metadata`→TEXT、`thread_id`/`checkpoint_ns`/`checkpoint_id` `TEXT`→`VARCHAR(N)`、索引语法改写、补中文注释 | **自研 saver** | **High** |
| `checkpoint_blobs` | 通道值 blob | **`BYTEA`**、`TEXT` 主键、`CONCURRENTLY` | `ag_checkpoint_blob` | `blob`→ 方案 C（对象存储 + 引用列）或 TEXT/LONGTEXT；主键列收紧为 `VARCHAR(N)` | **自研 saver + 对象存储客户端** | **High** |
| `checkpoint_writes` | 待处理写入 | **`BYTEA NOT NULL`**、`TEXT` 主键、`CONCURRENTLY` | `ag_checkpoint_write` | 同上 + `task_path` | 同上 | **High** |
| `checkpoint_migrations` | LangGraph 自身版本表 | 无 | `ag_checkpoint_migration` | 改名即可（但由第三方 `setup()` 管理 → 需自研） | 自研 saver | **Moderate** |
| `store` | LangGraph Store 文档 | **`value JSON`**、`text_pattern_ops` 索引、`unnest(%s::text[])` | `ag_store` | `value`→TEXT、前缀索引用合适 collation | 自研 store（仅需 `get`/`put`/`delete`/`search`） | **Moderate** |

---

## 15. Index Analysis

### 15.1 命名规范改造

规范：所有 index / constraint 名 ≤ 100 字符；unique → `uk_<field_name>`；
普通 → `idx_<field_name>`；composite 用简短可读名。

**现有 → 目标映射（基线索引，来自 `bootstrap.py:215-249`）**

| 现名 | 目标名 | 备注 |
| --- | --- | --- |
| `ix_users_email` | `uk_users_email` | `users.email` 是 `unique=True` → 唯一 |
| `idx_users_oauth_identity` | `uk_users_oauth_identity` | partial unique → 生成列唯一 |
| `ix_runs_thread_id` | `idx_runs_thread_id` | |
| `ix_runs_thread_status` | `idx_runs_thread_status` | |
| `ix_runs_user_id` | `idx_runs_user_id` | |
| `ix_runs_lease` | `idx_runs_lease` | |
| `uq_runs_idempotency_key` | `uk_runs_idempotency_key` | |
| `uq_runs_thread_active` | `uk_runs_thread_active` | 生成列唯一索引 |
| `ix_run_events_user_id` | `idx_run_events_user_id` | |
| `ix_events_thread_cat_seq` | `idx_events_thread_cat_seq` | |
| `ix_events_run` | `idx_events_run` | |
| `uq_events_thread_seq` | `uk_events_thread_seq` | |
| `ix_threads_meta_assistant_id` | `idx_threads_meta_assistant_id` | |
| `ix_threads_meta_user_id` | `idx_threads_meta_user_id` | |
| `ix_feedback_run_id` / `ix_feedback_thread_id` / `ix_feedback_user_id` | `idx_feedback_*` | |
| `ix_channel_connections_owner_user_id` / `_provider` | `idx_channel_connections_*` | |
| `uq_channel_connection_active_identity` | `uk_channel_connection_active_identity` | 需核对是否 partial |
| `idx_channel_connections_event_lookup` | `idx_channel_connections_event_lookup` | 已合规 |
| `ix_channel_conversations_*`（4 个） | `idx_channel_conversations_*` | |
| `ix_channel_oauth_states_owner_user_id` / `_provider` | `idx_channel_oauth_states_*` | |
| 后续 revision 引入的索引 | 按同规则重命名 | 见各 `versions/00NN_*.py` |

### 15.2 需要保留的查询覆盖（不得因去重而破坏）

按任务要求，逐项核对"filter / join / ordering / relationship lookup"四类覆盖：

| 查询模式 | 位置 | 依赖索引 |
| --- | --- | --- |
| 每线程一个活跃 run（唯一性 + reject 策略） | `runtime/runs/manager.py`、`runtime/runs/store/base.py` | `uk_runs_thread_active`（生成列） |
| 租约过期扫描 | `runtime/runs/*`、`scheduled_task_runs/sql.py:569-572` | `idx_runs_lease` |
| 事件流分页（`thread_id` + `category` + `seq`） | `runtime/events/store/db.py` | `idx_events_thread_cat_seq`（leftmost prefix 覆盖 `thread_id`） |
| 事件序号唯一性 | 同上 | `uk_events_thread_seq` |
| 线程列表按用户 + 助手过滤 | `persistence/thread_meta/sql.py` | `idx_threads_meta_user_id`、`idx_threads_meta_assistant_id` |
| due-task claim（`next_run_at` + lease 过期） | `persistence/scheduled_tasks/sql.py:320-332` | **需核对 `next_run_at` 是否有索引** ⚠️ |
| occurrence 队列 claim（`status='queued'` + `created_at`） | `persistence/scheduled_task_runs/sql.py:270-285` | **需核对 `(status, created_at)` 索引** ⚠️ |
| MCP poll claim（`next_poll_at` + lease） | `persistence/mcp_tasks/sql.py:220-230` | **需核对 `next_poll_at` 索引** ⚠️ |
| MCP cancel claim（`next_cancel_at`） | `mcp_tasks/sql.py:370-376` | ⚠️ |
| MCP notification claim（`next_notification_at`） | `mcp_tasks/sql.py:470-480` | ⚠️ |
| OAuth identity 唯一性 | `app/gateway/auth/repositories/sqlite.py` | `uk_users_oauth_identity`（生成列） |
| webhook 去重 | `app/channels/dedupe_store.py` | 4 列复合唯一索引 `uk_webhook_deliveries_key` |
| run → scheduled occurrence 反查（`metadata_json`） | `scheduled_task_runs/sql.py:847` | **拆列后建 `idx_runs_scheduled_task_run_id`** |
| credential / conversation 按 connection 反查 | `persistence/channel_connections/sql.py` | `idx_channel_credentials_connection_id`、`idx_channel_conversations_connection_id` |

### 15.3 Partial Index 的生成列替代方案（关键）

**`uq_runs_thread_active`（`run/model.py:56-70`）**

```sql
-- 设计示意（不在本阶段执行）
active_thread_id VARCHAR(64)
  GENERATED ALWAYS AS (IF(status IN ('pending','running'), thread_id, NULL)) STORED,
UNIQUE KEY uk_runs_thread_active (active_thread_id)
```

- MySQL 唯一索引**允许任意多个 `NULL`** → 终态行（`success`/`error`/…）不参与唯一性，
  与 PG 的 `WHERE status IN ('pending','running')` **精确等价**。
- ⚠️ **注意**：`app/gateway/auth/repositories/sqlite.py` 依赖的 `idx_users_oauth_identity`
  同理可用生成列，但**索引名会被 MySQL 1062 错误带出**，判定逻辑需改写。

**`idx_users_oauth_identity`（`user/model.py:66-84`）**

```sql
oauth_identity_key VARCHAR(160)
  GENERATED ALWAYS AS (
    IF(oauth_provider IS NOT NULL AND oauth_id IS NOT NULL,
       CONCAT(oauth_provider, ':', oauth_id), NULL)) STORED,
UNIQUE KEY uk_users_oauth_identity (oauth_identity_key)
```

- 或使用 **函数索引**（8.0.13+，8.0.24 可用）：
  `UNIQUE KEY uk_users_oauth_identity ((IF(oauth_provider IS NOT NULL AND oauth_id IS NOT NULL, CONCAT(oauth_provider,':',oauth_id), NULL)))`
- 二选一，需权衡：生成列更可读（可加中文 COMMENT），函数索引无需新增列。

### 15.4 需要新增的索引（现在缺失或靠 FK 隐式提供）

| 目标索引 | 理由 |
| --- | --- |
| `idx_scheduled_tasks_next_run_at` | due-task claim 的 `ORDER BY next_run_at` + `SKIP LOCKED`（MySQL 无索引会锁全表） |
| `idx_scheduled_task_runs_status_created` | occurrence 队列 claim 的 `WHERE status='queued' ORDER BY attempt_count, created_at, id`（现有 `0007` 只建了活跃去重索引） |
| `idx_mcp_tasks_next_poll_at` | poll claim |
| `idx_mcp_tasks_next_cancel_at` | cancel claim |
| `idx_mcp_tasks_next_notification_at` | notification claim |
| `idx_channel_credentials_connection_id` | 替代被删除的 FK 隐式索引 |
| `idx_subagent_batch_items_batch_id` | 替代被删除的 FK 隐式索引 |
| `idx_runs_scheduled_task_run_id` | 替代 `metadata_json ->> 'scheduled_task_run_id'` 过滤 |
| `idx_runs_regenerate_from_run_id` | 替代 `metadata_json ->> 'regenerate_from_run_id'` 过滤 |
| `idx_threads_meta_pinned_archived` | 替代 `JsonMatch` 的置顶/归档排序 |

### 15.5 需要删除的冗余索引

| 候选 | 理由 | 前置校验 |
| --- | --- | --- |
| `ix_runs_thread_id` | `uk_runs_thread_active`（生成列）不能覆盖 `thread_id` 等值查询，因为生成列对终态行为 `NULL`。**结论：不能删** | — |
| `ix_threads_meta_user_id` | 若新增 `idx_threads_meta_user_status_activity`（复合），leftmost prefix 覆盖 | 需确认无 `user_id` 单独查询 |
| `ix_feedback_thread_id` | 若查询总是带 `thread_id` + `user_id` | 需确认 |
| `idx_users_oauth_identity` 与 `ix_users_email` | 无关，不重复 | — |
| `ix_run_events_user_id` | 若与 `idx_events_thread_cat_seq` 无重叠（不同前导列）→ 保留 | — |

**结论**：本次迁移**不建议主动删除任何现有索引**（除重命名外）。
理由是 MySQL 的锁行为对索引缺失更敏感（§13.2），删除索引的风险高于收益。
若确需去重，应在 MySQL 上跑 `EXPLAIN` + 锁范围实测后再决定。

---

## 16. Third-party Library Compatibility

### 16.1 判定表

| 组件 | 版本 | 是否"支持 MySQL" | 是否满足本项目 MySQL 规范 | 判定 |
| --- | --- | --- | --- | --- |
| `langgraph-checkpoint-postgres` | 3.1.1 | ❌ 不支持（PG 专用） | ❌ | **不适用**（当前实现，迁移时移除） |
| `langgraph-checkpoint-sqlite` | 3.1.1 | ❌ | ❌ | 不适用 |
| **`langgraph-checkpoint-mysql`** | **3.0.0** | ✅ 支持（MySQL ≥ 8.0.19） | ❌ **不满足** | **需要定制 / 不兼容当前 Schema 规范** |
| `sqlalchemy` | 2.0.49 | ✅ 官方 MySQL 方言 | 取决于我们写的 DDL | 可用 |
| `alembic` | 1.18.4 | ✅ | 取决于我们写的 revision | 可用 |
| `asyncpg` | 0.31.0 | ❌ PG 专用 | — | 迁移时移除 |
| `psycopg` / `psycopg-pool` | 3.3.3 / 3.3.0 | ❌ PG 专用 | — | 迁移时移除 |
| MySQL 驱动 | — | — | — | **需新增**（见 §20 Q3） |

### 16.2 `langgraph-checkpoint-mysql` 3.0.0 违规明细

| 规范项 | 该包的实现 | 违规 |
| --- | --- | --- |
| 禁止 `JSON` | `checkpoints.checkpoint JSON NOT NULL`、`checkpoints.metadata JSON NOT NULL`、`store.value JSON NOT NULL` | ❌ |
| 禁止 `BLOB`/`LONGBLOB` | `checkpoint_blobs.blob LONGBLOB`、`checkpoint_writes.blob LONGBLOB NOT NULL` | ❌ |
| 禁止 specialized types | `checkpoint_ns_hash BINARY(16)` | ❌ |
| 表名 `ag_` 前缀 | `checkpoints` / `checkpoint_blobs` / `checkpoint_writes` / `checkpoint_migrations` / `store` / `store_migrations` | ❌ |
| 禁止 FK | 无 FK | ✅ |
| 至少一个 PK | 每表都有 | ✅ |
| 中文 COMMENT | 无任何 COMMENT | ❌ |
| 索引命名 `uk_`/`idx_` | `checkpoints_thread_id_idx` 等 | ❌ |
| 禁止数据库专有业务逻辑 | 大量 `json_table` / `json_keys` / `json_extract` / `json_unquote` / `json_arrayagg` / `json_contains` / `UNHEX(MD5())` | ❌ |
| 官方支持声明 | 第三方（作者 Theodore Ni，非 LangChain 官方） | ⚠️ 需明确标注为第三方 |

**结论**：该包在**功能上**可以让 LangGraph 跑在 MySQL 上，
但在**本项目规范下不可用**。且因为它的核心 SQL 依赖 JSON 函数，
"只改列类型"的 fork 方案会导致 SQL 全面重写 —— **性价比低于自研**。

### 16.3 其它需要注意的第三方耦合

| 组件 | 耦合点 | 迁移影响 |
| --- | --- | --- |
| `langgraph-api` 0.10.0 | Gateway 内嵌 LangGraph 运行时，通过 `checkpointer` 抽象使用 saver | 只要实现 `BaseCheckpointSaver` 契约即可替换 |
| `langgraph-runtime-inmem` 0.30.0 | 注释提到 "Standalone Studio's pre-runtime persistence repair is validated against the 0.30.0 store lifecycle" | 替换 Store 时需复核该修复逻辑 |
| `deerflow/checkpoint_patches.py` | 猴子补丁 `InMemorySaver.get_delta_channel_history` 与 `BinaryOperatorAggregate.update`，**已做版本守卫**（`_PATCH_VALIDATED_LANGGRAPH_VERSION = Version("1.2.9")`） | 与数据库无关，不受影响 |
| `app/gateway/auth/repositories/sqlite.py:36-39` | 依赖 **SQLAlchemy asyncpg 方言**包装后的异常形状（`exc.orig` 不是原始驱动错误） | 换驱动后该判定逻辑必须重写 |

---

## 17. Migration Risk

### 17.1 三大风险（本任务要求）

#### 风险 1：Checkpoint 持久化层没有合规实现（**最高**）

- **事实**：官方无 MySQL saver；第三方包用 `JSON` + `LONGBLOB`；
  自研需重写 `SELECT_SQL`（含 `jsonb_each_text` / `array_agg` / `::bytea`）、
  `UPSERT_*`（含 `ON CONFLICT`）、DeltaChannel 两阶段 SQL、以及 `setup()` 迁移链。
- **为什么危险**：这是**唯一可能推翻整个方案**的点。
  若 blobs 采用方案 C（对象存储），则写入路径从 1 次 DB 提交变成
  "对象存储写 + DB 提交"，必须处理部分失败与孤儿 GC；
  若采用 TEXT/LONGTEXT，则与"禁止复杂类型"的规范存在张力（`TEXT` 上限 64KB）。
- **缓解**：先做 **Checkpoint 层的可替换接口 + 最小可用 MySQL 实现**，
  用现有测试套件（`tests/test_delta_channel_checkpointers.py`、
  `tests/test_run_worker_delta_resume.py`、`tests/test_multi_worker_run_ownership.py`）
  做回归，再决定是否继续。

#### 风险 2：并发语义变化（**高**）

- **事实**：4 处 advisory lock、2 处 `RETURNING`、1 处 `ON CONFLICT … WHERE`、
  2 处 partial index、1 处 PG 专有行锁强度假设（`mcp_tasks/sql.py:166-168`）、
  默认隔离级别从 RC 变 RR。
- **为什么危险**：这些是"能跑但可能静默错误"的一类问题。
  最典型的静默错误场景：
  - `dedupe_store` 的 TTL 重入判定在 MySQL 上被简化后，
    过期的 webhook 不再被重新接纳（重复消息被永久丢弃）；
  - `scheduled_task_runs` 的 `occurrence_seq` 分配改用
    `LAST_INSERT_ID(expr)` 后与 `run_events` 的 `AUTO_INCREMENT` 语义互相污染；
  - RR 下的 gap lock 让 claim 语句的"跳过"行为与 PG 不同，
    导致偶发死锁（`1213`）而非正确的跳过。
- **缓解**：为每个并发原语写**MySQL 专属的并发回归测试**；
  显式 `SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED`；
  用 `scripts/benchmark/concurrency/worker.py` 做双 Worker 压测。

#### 风险 3：Schema 层的隐性约束（**中高**）

- **事实**：
  1. InnoDB 索引键长上限 **3072 字节**（utf8mb4 → 单列最多 768 字符）。
     LangGraph 的 `checkpoints` PK 是 `(TEXT, TEXT, TEXT)`，
     第三方 MySQL 包改成 `VARCHAR(150)×3` + 后来加哈希列 —— 说明**键长是真实约束**。
  2. `TEXT` 不能作主键，必须转 `VARCHAR(N)`。
  3. `DATETIME` 默认精度 0 → **截断亚秒**，破坏 `scheduled_task_runs` 的 FIFO
     排序与 `expire_queued_runs` 的过期判定。
  4. `DateTime(timezone=True)` 在 MySQL 上**静默丢弃时区**。
  5. `TEXT` 上限 64KB 与 checkpoint blob 的体积冲突。
- **为什么危险**：这类问题在开发环境（小数据、单进程）**不会暴露**，
  只会在生产（长会话、多 Worker、跨时区）暴露。
- **缓解**：所有时间列显式 `DATETIME(6)`；为 checkpoint 主键列
  确定并**断言**最大长度（`checkpoint_ns` 会因嵌套子图增长）；
  在应用层统一 UTC-naive 写入边界。

### 17.2 风险清单（按严重度）

| # | 风险 | 严重度 | 可能性 | 缓解 |
| --- | --- | --- | --- | --- |
| 1 | Checkpoint 自研 saver 的功能/性能不达标 | 极高 | 中 | 分阶段验证；保留 PG 回退能力 |
| 2 | checkpoint blob 超出 TEXT 上限 | 高 | 高 | 方案 C（对象存储）或明确放宽到 LONGTEXT |
| 3 | advisory lock 替换后调度器重复触发/漏触发 | 高 | 中 | 锁表 sentinel + 并发回归测试 |
| 4 | `RETURNING` 替换后序号重复/跳号 | 高 | 中 | 两步法（`FOR UPDATE` + `UPDATE`）+ 唯一约束兜底 |
| 5 | RR 隔离级别导致死锁率上升 | 中高 | 高 | 显式 RC + 锁顺序审计 + 死锁重试 |
| 6 | 时间精度截断导致 FIFO/过期判定错误 | 中高 | 高 | `DATETIME(6)` + 边界转换 + 测试 |
| 7 | partial index 生成列 workaround 的锁开销 | 中 | 中 | 压测写入路径 |
| 8 | 20 张表 + bootstrap 常量重命名遗漏 | 中 | 中 | 用编译期/启动期断言；一次性 grep 清单 |
| 9 | 级联删除从 FK 移到应用层后出现孤儿行 | 中 | 中 | 显式级联实现 + 孤儿巡检脚本 |
| 10 | 移除 PG 依赖后 Helm/文档/CI 不一致 | 低 | 高 | 文档与 chart 同步更新 |

---

## 18. Recommended Target Database Boundary

**基于代码调查结果的推荐边界**（与任务给出的候选方向基本一致，
但**第 2 项由现状推导而非预设**）：

```
Agent Runtime
│
├── MySQL 8.0.24
│   ├── Application Data        ag_runs / ag_run_events / ag_threads_meta / ag_users /
│   │                           ag_user_preferences / ag_agents / ag_projects /
│   │                           ag_feedback / ag_personal_access_tokens /
│   │                           ag_managed_subagents / ag_subagent_*
│   ├── Thread / Run            ag_threads_meta / ag_runs / ag_run_events
│   ├── Checkpoint              ag_checkpoint / ag_checkpoint_blob / ag_checkpoint_write
│   │                           （blob 采用"小内联 TEXT + 大对象引用"混合）
│   ├── Memory Metadata         ag_store（LangGraph Store，仅 agent 文档）
│   └── Operational Data        ag_scheduled_tasks / ag_scheduled_task_runs /
│                               ag_mcp_tasks / ag_webhook_deliveries /
│                               ag_channel_*
│
├── Object Storage
│   ├── Artifacts               大 tool 结果（已有 .tool-results 雏形）
│   └── Large Payload           checkpoint 大 blob、mcp_tasks.result_artifact、
│                               subagent_batch_items.result、uploads
│
├── Knowledge Service
│   └── Vector / RAG            当前已是外部 RAGFlow HTTP；本地检索走 SQLite FTS5
│                               （deermem / task_continuity）—— **不进 MySQL**
│
└── Remote Sandbox
    └── Temporary Workspace     LocalSandboxProvider / E2B / boxlite / tenki / opensandbox
```

**推导依据**：

1. **Checkpoint 进 MySQL 是可行的，但必须自研 + 混合 blob 策略**
   （因为规范禁止 BLOB 且 TEXT 有 64KB 上限，而 `messages` 通道是累积 reducer）。
2. **Vector 不进 MySQL**：现状已经满足（pgvector 未启用），不应为了"统一数据库"而引入。
3. **Object Storage 是必需的**，不是可选的：它是解决 checkpoint blob 与 TEXT 上限冲突的
   唯一合规路径。
4. **不需要引入 Redis 来替代 advisory lock**：Redis 已存在于项目（stream bridge /
   checkpoint cache / dev compose），但它**不能**替代数据库级事务锁语义；
   若采用"锁表"方案可以完全不引入新依赖。

---

## 19. Recommended Migration Sequence

> 以下步骤为**建议顺序**，本任务不执行其中任何一步。

### 阶段 0：准备（无 Schema 变更）

1. 冻结基线：记录 `alembic head = 0023_user_preferences`，确认 0001–0023 不可变。
2. 建立"MySQL 规范检查清单"（§7 的规则化），作为后续所有 revision 的验收标准。
3. 明确 §20 的待确认项（尤其 Q1 migration 载体、Q2 表名映射、Q3 驱动选择）。

### 阶段 1：抽离持久化边界（仍跑 PG）

4. 把 Checkpointer / Store 的构造从 `provider.py` / `async_provider.py` 的具体类
   抽象为"按 `config.type` 分派的工厂"，新增 `mysql` 分支（先留空实现）。
5. 把 §10 的并发原语抽象为显式接口：
   `acquire_txn_lock(key)` / `allocate_sequence(table, col, where)` /
   `conditional_upsert(...)`，为每种后端提供实现。**这一步让并发语义可测试**。
6. 引入时间写入边界（UTC aware → naive UTC）与 `DATETIME(6)` 约定。

### 阶段 2：Checkpoint / Store 的 MySQL 实现（最高风险，最先做）

7. 实现 `ag_checkpoint` / `ag_checkpoint_blob` / `ag_checkpoint_write` /
   `ag_checkpoint_migration` 的 MySQL 方言 saver（`BaseCheckpointSaver` 子类），
   含中文 COMMENT、`ag_` 前缀、`uk_`/`idx_` 索引命名。
8. 实现 `ag_store` 的最小 MySQL store（`get` / `put` / `delete` / `search`）。
9. 决定 blob 策略（方案 A `TEXT`+内联 或 方案 C 对象存储 + 引用），
   并实现对应读写路径与孤儿 GC。
10. 用现有测试回归：
    `tests/test_delta_channel_checkpointers.py`、`tests/test_run_worker_delta_resume.py`、
    `tests/test_run_worker_rollback.py`、`tests/test_run_duration_checkpoint.py`。
11. **决策点**：若此阶段无法在不违反规范的前提下达标，方案退化为
    `Not currently recommended`。

### 阶段 3：Application Data 的 MySQL baseline

12. 创建 `0024_mysql_baseline.py`：以 MySQL 规范重建全部 20 张表
    （`ag_` 前缀、TEXT 序列化、生成列唯一索引、`DATETIME(6)`、
    无 FK、`DEFAULT` 补齐、中文 COMMENT、`uk_`/`idx_` 命名）。
13. 创建 `0025_mysql_backfill_not_null.py`：执行 §7.3 的 backfill，
    然后把可安全 NOT NULL 的列切 `NOT NULL`。
14. 创建 `0026_mysql_cascade_delete.py`：为 4 处被移除的 FK 提供应用层级联删除，
    并加入孤儿巡检脚本。
15. 修改 ORM 模型（`__tablename__`、去 `ForeignKey`、`sa.JSON`→`sa.Text`、
    `DateTime(timezone=True)`→`DATETIME(6)`、补 `server_default` 与 `comment=`）。
16. 重写 §5.2 的 6 类违规查询为应用层逻辑，并删除 `persistence/json_compat.py`
    的方言 hack（或整体废弃该模块）。

### 阶段 4：并发原语替换

17. 替换 4 处 advisory lock（锁表 sentinel 或 `GET_LOCK` + 严格生命周期管理）。
18. 替换 2 处 `RETURNING`（`LAST_INSERT_ID(expr)` 或两步法）。
19. 替换 `dedupe_store` 的条件 upsert。
20. 显式设置 `READ COMMITTED`；审计全部 `with_for_update` 的索引可用性。
21. 重写 `app/gateway/auth/repositories/sqlite.py` 的错误码判定。

### 阶段 5：基础设施与验证

22. 新增 MySQL 驱动依赖 + `mysql` extra；移除 `postgres` extra 或保留双后端。
23. `persistence/engine.py`：新增 MySQL engine kwargs（`pool_pre_ping`、
    `pool_recycle`、`max_execution_time`），移除 `_auto_create_postgres_db` 或改为
    `CREATE DATABASE IF NOT EXISTS`。
24. `app/gateway/health.py`：新增 `mysql` 探针。
25. `deploy/helm/`：新增 MySQL StatefulSet 或 external DSN 支持；
    更新 `values.yaml` / `NOTES.txt` / `postgres-secret.yaml`。
26. 新增 MySQL 专属集成测试（对应现有 8 个 PG 测试文件）。
27. 文档：更新 `backend/AGENTS.md`、`config.example.yaml`、`README*`。

---

## 20. Open Questions

| # | 问题 | 为什么需要确认 | 建议默认 |
| --- | --- | --- | --- |
| **Q1** | Migration 载体：任务要求 `00N_xxx.sql`，项目现为 Alembic Python revision `00NN_xxx.py`。是否改用裸 SQL？ | 改用 SQL 需要重写 `bootstrap.py` 的整个状态机（`ScriptDirectory` / `_get_head_revision` / `_KNOWN_REVISIONS`），并放弃 `safe_add_column` 幂等 helper 与 `_env_filters` 的 autogenerate 保护 | **保持 Alembic revision，编号从 0024 续** |
| **Q2** | 表名映射：任务示例为 `ag_thread` / `ag_memory` / `ag_run`，而现有表是 `threads_meta` / `runs`。是机械加前缀还是重命名？ | 机械加前缀可追溯（grep 友好）；重命名更贴合语义但要同步 20 张表的所有引用 | **机械加前缀**（`ag_threads_meta` / `ag_runs`），LangGraph 表用 `ag_checkpoint` / `ag_checkpoint_blob` / `ag_checkpoint_write` / `ag_checkpoint_migration` / `ag_store` |
| **Q3** | MySQL 驱动选择：`asyncmy`（异步、性能好、维护活跃）/ `aiomysql`（成熟但慢）/ `mysqlclient`+`PyMySQL`（同步） | 影响 SQLAlchemy URL 方言（`mysql+asyncmy://`）、连接池实现、错误类型判定（`exc.orig`） | **`asyncmy`**（异步主路径）+ `PyMySQL`（同步路径，与 `app_sync_sqlalchemy_url` 对齐） |
| **Q4** | `personal_access_tokens.scopes`（JSON 数组）：TEXT 存 JSON，还是逗号分隔 `VARCHAR`？ | 影响是否需要 `LIKE` 过滤 | **TEXT**（与其它列一致，避免自造分隔符协议） |
| **Q5** | Checkpoint blob 策略：内联 `TEXT`/`LONGTEXT` 还是对象存储？ | 决定是否引入新基础设施；决定是否放宽"禁止复杂类型"的规范 | **混合**：小 blob 内联 `TEXT`，超阈值走对象存储 |
| **Q6** | 是否允许 `MEDIUMTEXT` / `LONGTEXT`？ | 任务规范的"优先使用"清单只列了 `TEXT`；但 64KB 上限与 checkpoint blob 冲突 | 需明确决策。若不允许，则 blob 必须走对象存储 |
| **Q7** | 是否保留 PostgreSQL 作为可选后端（双后端）？ | 影响是否必须为每个并发原语保留两套实现 | **短期保留**（降低回滚风险），中期再收敛 |
| **Q8** | MySQL 是否允许 `GET_LOCK`？ | 若允许，advisory lock 替换成本下降，但连接池下的生命周期管理需要极小心 | **不允许**，改用锁表 sentinel row |
| **Q9** | `alembic_version` 是否也要 `ag_` 前缀？ | 规范说"所有物理表名"；但 Alembic 默认名有工具链约定 | **要**（配置 `version_table`），并同步 `bootstrap.py` |
| **Q10** | MySQL 版本是否严格锁定 8.0.24（不使用 8.0.28+ 的 `TIMESTAMP` 扩展或 8.4 的 `RETURNING`… 若未来有）？ | 影响可用语法 | **严格 8.0.24**，不使用 8.0.25+ 特性 |

---

## 21. Final Feasibility Assessment

### 21.1 逐项回答（任务 §20 的 13 个问题）

**1. 当前项目是否能够完全移除 PostgreSQL？**
**能，但不是"零成本移除"。** 应用层（20 张表、ORM、仓储）可以在不改变领域模型的前提下迁移。
Checkpoint / Store 层目前**完全依赖** `langgraph-checkpoint-postgres` 3.1.1 的
`PostgresSaver` / `AsyncPostgresSaver` / `PostgresStore` / `AsyncPostgresStore`，
其 SQL 与 DDL 硬编码 PostgreSQL 语义（`JSONB`、`BYTEA`、`jsonb_each_text`、
`array_agg`、`ANY(%s)`、`CREATE INDEX CONCURRENTLY`、`%s` 占位符）。
必须用自研实现替换，这不是"移除依赖"，而是"重写一个持久化组件"。
另外 4 处 advisory lock / 2 处 `RETURNING` / 2 处 partial index 也必须重写。

**2. 是否能够使用 MySQL 8.0.24？**
**能。** 8.0.24 具备全部必需的 SQL 能力：`FOR UPDATE`、`FOR SHARE`、
`SKIP LOCKED`、CTE、窗口函数、函数索引（8.0.13+）、生成列、
`ON DUPLICATE KEY UPDATE`、行别名 `AS new`（8.0.19+）、`LAST_INSERT_ID(expr)`。
不具备的只有 `RETURNING`、partial index、事务级 advisory lock、
`CREATE INDEX CONCURRENTLY`、`idle_in_transaction_session_timeout` —— 这五项都有替代路径。

**3. 是否能够严格满足本任务规定的 MySQL Schema 规范？**
- **Application Data：能。** 24 个 JSON 列中 22 个转 `TEXT`，2 张表的 metadata 拆 scalar 列；
  4 处 FK 移除；2 处 partial index 用生成列/函数索引精确等价替代；
  20 张表加 `ag_` 前缀；补齐中文 COMMENT 与 `DEFAULT`；索引重命名为 `uk_`/`idx_`。
- **Checkpoint / Store：不能直接复用任何现成实现。**
  官方无 MySQL saver；第三方 `langgraph-checkpoint-mysql` 3.0.0 使用
  `JSON` + `LONGBLOB` + `BINARY(16)`，**违反规范**。
  必须自研 `BaseCheckpointSaver` / `BaseStore` 的 MySQL 实现，
  并且必须解决 `TEXT` 64KB 上限与累积型 `messages` 通道 blob 的冲突
  （要么放宽到 `MEDIUMTEXT`/`LONGTEXT`，要么走对象存储）。

**4. Application Data 迁移难度？**
**Moderate。** 主要是机械改造（类型映射、命名、注释、DEFAULT），
但有 3 个非机械点：`threads_meta` / `runs` 的 JSON 过滤需拆列、
4 处级联删除需从 FK 移到应用层、时间精度与时区需要系统性审计。

**5. LangGraph Checkpoint 迁移难度？**
**Hard。** 需要自研 saver（含 DDL、`SELECT` 聚合、UPSERT、DeltaChannel 两阶段查询、
`setup()` 迁移链），并决定 blob 落地策略。这是整个方案的技术风险中心。

**6. LangGraph Store / Memory 迁移难度？**
**Moderate（自研）→ Hard（若使用第三方）。**
Store 的实际使用面极窄（`setup_agent_tool.py:63` 的 `get` + 装配路径），
自研一个只支持 `get/put/delete/search(prefix)` 的 `ag_store` 成本可控。
**注意**：DeerFlow 的"长期记忆"（`deermem`）与 PostgreSQL **完全无关**
（本地文件 + SQLite FTS5），不在迁移范围内。

**7. Queue / Scheduler 迁移难度？**
**Hard。** 四套子系统（Scheduler / MCP Tasks / Run ownership / Inbound dedupe）
分别依赖 advisory lock、`RETURNING`、partial unique index、`SKIP LOCKED`、
PG 专有行锁强度模型。且默认隔离级别从 RC 变 RR 会引入 gap lock 与新的死锁模式。
必须为每个原语写 MySQL 专属并发测试。

**8. Knowledge / Vector 应如何处理？**
**保持现状，不进 MySQL。** 当前**未使用 pgvector**（`PostgresStore` 未配置 embeddings，
向量迁移从未执行）；RAG 走外部 RAGFlow HTTP；本地检索走 SQLite FTS5。
应当固化为"Knowledge Service 独立于 Agent Runtime 数据库"的边界。

**9. 哪些第三方组件虽然支持 MySQL，但违反当前 MySQL 规范？**
- **`langgraph-checkpoint-mysql` 3.0.0**（第三方，非官方）：
  `JSON` 列、`LONGBLOB` 列、`BINARY(16)`、无 `ag_` 前缀、无中文 COMMENT、
  索引命名不符、大量 JSON 函数。
- 附带说明：`langgraph-checkpoint-postgres` 3.1.1 虽然当前在用，但它是 PG 专用，
  不属于"支持 MySQL 但不合规"这一类。

**10. 是否存在必须继续保留 PostgreSQL 的能力？**
**严格来说没有"能力"必须保留，但有两个"实现便利"会失去：**
- **事务级 advisory lock**（`pg_advisory_xact_lock`）：MySQL 无等价物，
  `GET_LOCK` 是连接级。若改用锁表 sentinel，需要额外的表和一套锁生命周期管理。
- **Partial / Filtered Index**：MySQL 无原生支持，必须用生成列或函数索引模拟
  （语义可等价，但 DDL 可读性与锁开销不同）。
除此之外，PG 的其它用法（`RETURNING`、`ON CONFLICT`、`JSONB`、`BYTEA`）都有替代路径。

**11. 哪些数据应该迁移到 Object Storage，而不是 MySQL？**
1. **Checkpoint 大 blob**（`checkpoint_blobs.blob` / `checkpoint_writes.blob`）——
   最高优先，因为 `messages` 是累积 reducer，blob 随会话长度增长，必然超出 `TEXT` 上限。
2. `mcp_tasks.result_artifact`（`max_result_bytes: 65536`）。
3. `subagent_batch_items.result` / `result_preview`（`max_result_chars: 100000`）。
4. 大 tool 结果（已有 `.tool-results` 卸载，阈值 12000 字符）。
5. Uploads（单文件上限 50MB）与 agent workspace 变更产物。

**12. 最大的三个迁移风险是什么？**
1. **Checkpoint 持久化层没有合规实现**（§17.1 风险 1）—— 唯一可能推翻整个方案的点。
2. **并发语义变化**（§17.1 风险 2）—— advisory lock / `RETURNING` /
   条件 upsert / partial index / RC→RR，属于"能跑但可能静默错误"。
3. **Schema 层隐性约束**（§17.1 风险 3）—— InnoDB 3072 字节键长上限、
   `TEXT` 不能作主键、`DATETIME` 截断亚秒、`timezone=True` 静默丢弃时区、
   `TEXT` 64KB 与 blob 体积冲突。

**13. 推荐迁移顺序是什么？**
见 §19。核心原则：**先做 Checkpoint / Store 的 MySQL 实现（阶段 2，最高风险），
再做 Application Data（阶段 3），最后做并发原语替换与基础设施（阶段 4/5）**。
不要把顺序反过来 —— 如果阶段 2 失败，前面所有 Application Data 的工作都白做。

### 21.2 整体结论

> ## `Feasible with significant changes`

**判定依据（代码证据）**：

| 支撑 `Feasible` 的证据 | 支撑 `significant changes` 的证据 |
| --- | --- |
| MySQL 8.0.24 具备全部必需的 SQL 原语（除 `RETURNING`/partial index/advisory lock） | `langgraph-checkpoint-postgres` 3.1.1 的 DDL 使用 `JSONB` + `BYTEA`（§8.2 实测） |
| 应用侧已有"JSON 文本 + 类型标记"的序列化模式（`engine.py:25-27`、`events/store/db.py:80-89`） | LangGraph 官方**无** MySQL checkpointer/store（`.venv` 实测只有 base/memory/serde/sqlite） |
| 现有 `channel_connections/sql.py:401-404` 已示范应用层派生锁 key 的模式 | 第三方 `langgraph-checkpoint-mysql` 3.0.0 用 `JSON` + `LONGBLOB` + `BINARY(16)`（§8.4 实测） |
| 2 处 partial index 可用生成列精确等价替代 | 2 处 `RETURNING` + 1 处 `ON CONFLICT … WHERE` 无 MySQL 等价物（§10.2 #4/#5） |
| Vector / RAG **已经**在数据库之外（RAGFlow HTTP + SQLite FTS5） | 4 处 advisory lock 无事务级等价物（§10.2 #1/#2） |
| Store 使用面极窄（仅 `setup_agent_tool`） | 默认隔离级别 RC → RR，引入 gap lock 与新的死锁模式（§10.2 #12） |
| 项目已有幂等 migration helper 与三分支 bootstrap，可作为新 baseline 的骨架 | 20 张表 + bootstrap 常量 + health check + 8 个 PG 测试需要系统性改名（§7.2） |
| 项目已有 Object Storage 雏形（`.tool-results` 卸载） | checkpoint blob 体积与 `TEXT` 64KB 上限冲突，必须放宽类型或引入对象存储（§12.4 方案 A） |

**不推荐的情况**：如果组织无法接受
（a）自研并长期维护一个 MySQL checkpoint saver，或
（b）引入对象存储来承载 checkpoint 大 payload，
那么结论应当退回到 **`Not currently recommended`** ——
因为剩下唯一的选择是使用违反本项目 Schema 规范的第三方包，
那与本任务的核心目标（严格满足 MySQL 规范）直接冲突。

---

## 附录 A：本次调查使用的证据清单

| 类别 | 路径 |
| --- | --- |
| 依赖清单 | `backend/pyproject.toml`、`backend/packages/harness/pyproject.toml`、`backend/uv.lock`、`backend/.venv` 实测版本 |
| 配置模型 | `backend/packages/harness/deerflow/config/database_config.py`、`config/postgres_schema.py`、`config/checkpointer_config.py` |
| PostgreSQL 适配层 | `persistence/postgres_schema.py`、`persistence/engine.py`、`persistence/bootstrap.py` |
| ORM 模型 | `persistence/base.py`、`persistence/models/`、`persistence/*/model.py`（20 张表） |
| Migration | `persistence/migrations/versions/0001_baseline.py` … `0023_user_preferences.py`、`migrations/env.py`、`migrations/_helpers.py`、`migrations/_env_filters.py` |
| Checkpointer | `runtime/checkpointer/provider.py`、`runtime/checkpointer/async_provider.py`、`runtime/checkpointer/cached_saver.py`、`runtime/checkpoint_mode.py`、`checkpoint_patches.py` |
| Store | `runtime/store/provider.py`、`runtime/store/async_provider.py` |
| Checkpoint 缓存 | `runtime/checkpoint_cache/{base,memory,redis,provider}.py` |
| 并发原语 | `runtime/events/store/db.py`、`persistence/scheduled_task_runs/sql.py`、`persistence/scheduled_tasks/sql.py`、`persistence/mcp_tasks/sql.py`、`persistence/run/sql.py`、`persistence/channel_connections/sql.py`、`persistence/user/preferences.py`、`app/channels/dedupe_store.py` |
| JSON 方言 hack | `persistence/json_compat.py` |
| 健康检查 | `app/gateway/health.py`、`app/gateway/app.py:888-915` |
| 部署 | `deploy/helm/deer-flow/values.yaml`、`templates/postgres-statefulset.yaml`、`templates/NOTES.txt`、`docker/docker-compose*.yaml` |
| 配置模板 | `config.yaml`、`config.example.yaml:2286-2343` |
| LangGraph 第三方源码 | `langgraph_checkpoint_postgres-3.1.1-py3-none-any.whl`（`langgraph/checkpoint/postgres/base.py`、`shallow.py`、`langgraph/store/postgres/base.py`）、`.venv/.../langgraph/checkpoint/serde/jsonplus.py` |
| 第三方 MySQL 包 | `tjni/langgraph-checkpoint-mysql`（PyPI 3.0.0）`langgraph/checkpoint/mysql/base.py`、`langgraph/store/mysql/base.py` |
| 测试 | `tests/test_pg_schema_integration.py`、`tests/test_persistence_bootstrap_pg_lock.py`、`tests/test_multi_worker_postgres_gate.py`、`tests/test_scheduled_task_postgres.py`、`tests/test_mcp_task_postgres.py`、`tests/test_migration_0018_oauth_identity_pg_partial.py`、`tests/test_persistence_engine_postgres_config.py`、`tests/test_postgres_schema_helper.py`、`tests/test_run_event_store.py`、`tests/test_thread_meta_repo.py` |

## 附录 B：本任务未执行的动作（合规声明）

- ❌ 未修改任何生产代码
- ❌ 未修改任何数据库 Schema
- ❌ 未创建任何 migration
- ❌ 未替换数据库驱动
- ❌ 未新增数据库实现
- ❌ 未新增测试
- ❌ 未执行任何实际数据迁移
- ❌ 未修改 Docker 初始化文件（本项目无此类文件）
- ❌ 未运行与分析无关的测试套件
- ✅ 仅执行了只读代码搜索、依赖清单核对、已安装包版本探测，
  以及将第三方 wheel 下载到 `/tmp` 解压阅读（未安装、未写入仓库）
