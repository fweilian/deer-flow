# PostgreSQL → MySQL 8.0.24 Migration Goals

基于最新版 `mysql-migration-plan.md` 执行 PostgreSQL → MySQL 8.0.24 fresh-cutover。

## 全局约束

整个迁移严格遵循：

* 不迁移 PostgreSQL 历史数据；
* MySQL 从空库开始；
* 不做 dual-read / dual-write；
* 不做 backfill；
* 不设计 MySQL → PostgreSQL 数据回滚；
* PostgreSQL 最终完全删除；
* 生产 Runtime DB 账号没有 DDL 权限；
* 生产 Runtime 不执行：

  * `CREATE DATABASE`
  * `CREATE TABLE`
  * `ALTER TABLE`
  * `DROP TABLE`
  * `CREATE INDEX`
  * `create_all`
  * `alembic upgrade`
  * `stamp`
  * CheckpointSaver `setup()`
* 所有生产 DDL 由运维 / DBA 使用独立 migration 账号执行；
* Runtime 只连接、读写数据和验证 Schema/version；
* Schema 缺失或版本不匹配必须 fail closed；
* `checkpoint_channel_mode=full` 保持不变；
* 不引入 Redis checkpoint cache；
* 不引入 Checkpoint Object Storage overflow；
* 不新增通用 PG/MySQL dialect abstraction；
* 不做无关索引优化；
* 不做 PG/MySQL 全量双后端参数化测试；
* Optional Compliance 不阻塞 Core Migration。

实施顺序必须为：

`Goal 0 -> Goal 1 -> Goal 2 -> Goal 3 -> Goal 4 -> Goal 5`

Goal 1 未通过前，不允许进入 Goal 2。

---

# Goal 0 — Runtime Scope Cleanup

## 目标

在开始任何 MySQL Schema 实现之前，先删除本期明确不需要迁移的 Runtime 能力，使后续 MySQL baseline 只包含真正需要的模型。

本 Goal 不实现 MySQL。

## 0-A. 删除 Channel / GitHub Webhook

完整删除当前 DeerFlow Channel / GitHub Webhook Runtime，包括：

* `app/channels/`
* Channel routers
* `channel_connections`
* GitHub webhook router / dispatcher
* `gateway/github/`
* Channel 配置
* Channel persistence
* webhook delivery persistence
* Channel SDK 依赖
* Channel lifecycle / dependency injection
* Channel 专属测试

先处理 Channel 与 GitHub 的双向 import，再删除主体。

保留普通 Web/API Chat、SSE、Runtime StreamBridge。

注意：

名字里带 `channel` 的 LangGraph `DeltaChannel` 不是 IM Channel，不得误删。

必须保留相关 checkpoint regression tests。

## 0-B. 删除 `mcp_tasks`

正式删除 MCP 长任务 Runtime：

* `persistence/mcp_tasks/`
* `mcp/tasks/`
* `app/mcp_tasks/`
* `routers/mcp_tasks.py`
* `background_tasks_tool.py`
* `McpTasksConfig`
* lifespan / deps 注入
* `task_toolsets` 后台任务包装路径
* 相关测试

必须保留：

* 普通 MCP Tool；
* MCP Server；
* MCP OAuth；
* 普通同步 MCP 调用。

未来如重新需要 MCP 长任务，以新的 MySQL-native feature 重新实现，不恢复旧 PG persistence。

## 0-C. 删除 `subagent_batches`

正式删除 Batch SubAgent Runtime：

* `persistence/subagent_batches/`
* `subagent_batches`
* `subagent_batch_items`
* `batch_runtime`
* `batch_service`
* batch router
* `batch_task_tool`
* batch feature flag
* batch tool mounting
* batch prompt 文案
* batch tests

必须保留：

* 普通 `task`；
* Lead Agent → SubAgent 委派；
* `managed_subagents`。

未来若需要批量 SubAgent，以新的 MySQL-native feature 增量增加：

`000N_add_subagent_batches`

不得修改 `0001_mysql_baseline`。

## 0-D. 删除 LangGraph Store

删除：

* `runtime/store/provider.py`
* `runtime/store/async_provider.py`
* Store lifecycle
* `app.state.store`
* graph / agent 的无效 store 挂载
* orphan thread 历史迁移
* `reset_store`
* Store health probe
* Store re-export

不要创建 MySQL Store，也不要创建 `ag_store`。

### `_sqlite_utils.py`

不能跟 Store 一起删除。

将其中被 Checkpointer / Health 使用的 SQLite helper 移到合适的独立模块，例如：

`runtime/sqlite_utils.py`

或：

`runtime/checkpointer/sqlite_utils.py`

## 0-E. 保留 MemoryThreadMetaStore，但去掉 BaseStore

`database.backend=memory` 仍作为开发和测试能力保留。

将 `MemoryThreadMetaStore` 改为简单内部 dict backend。

只实现当前真实需要的最小接口：

* `aget`
* `aput`
* `adelete`
* `asearch`

不要为了它保留 LangGraph `BaseStore` abstraction。

## 0-F. 同步更新 Feature Inventory

同步修改 `feature-inventory.md`：

* `mcp_tasks`：已决定删除；
* `subagent_batches`：已决定删除；
* 不再描述为待评估 / B 类待启用能力；
* 记录“未来若有真实需求，以新功能形式重新实现”。

## 验证

只做与删除范围直接相关的测试。

至少验证：

* Gateway 可启动；
* Web/API Chat 不受影响；
* 普通 MCP Tool 可加载；
* 普通 SubAgent `task` 可用；
* Store 删除后 Gateway 主链无 Store 读取；
* memory thread metadata 测试通过；
* DeltaChannel checkpoint tests 仍存在并通过；
* Channel / GitHub / mcp_tasks / subagent_batches 无生产引用残留。

最后重新反射 ORM metadata。

目标结果：

* 12 张应用表；
* 139 列；
* `mcp_tasks` 不存在；
* `subagent_batches` 不存在；
* `subagent_batch_items` 不存在；
* 5 张 Channel/Webhook 表不存在于目标模型。

## 退出条件

只有满足以下条件才能进入 Goal 1：

1. 所有删除范围已完成；
2. Gateway 主链正常；
3. MCP 普通调用正常；
4. 普通 SubAgent 正常；
5. ORM 最终范围稳定；
6. 重新扫描 PostgreSQL dependencies 并保存最新结果；
7. Feature Inventory 与实际代码一致。

---

# Goal 1 — MySQL Compatibility & Risk Gates

## 目标

不做大规模迁移实现。

先解决所有可能推翻方案的高风险问题。

本 Goal 的核心产出是：

* Gate V1 关闭；
* Gate V5 关闭；
* 最终确认 CheckpointSaver 引入方式；
* 最终确认生产 migration 模型。

## 1-A. Gate V1 — MySQL 独立 Alembic Chain

实现 / 验证：

* PostgreSQL legacy chain 保持 immutable；
* MySQL 使用独立 script location；
* MySQL root：
  `0001_mysql_baseline`
* 默认继续使用：
  `alembic_version`
* 不建立通用 multi-chain framework；
* 不建立 chain registry；
* 不建立 per-chain class hierarchy。

重新检查：

* `_HEAD_REVISION`
* `_KNOWN_REVISIONS`

如果没有必要，删除生产 Runtime 对这些 cache 的依赖。

Runtime 最终只需要知道：

“当前连接的 MySQL revision 是否等于当前应用要求的 revision”。

生产 Runtime 不执行 migration。

生产 migration：

`DBA -> alembic upgrade head`

Runtime：

`read revision -> verify -> start/fail`

验证：

* PG chain 和 MySQL chain 互不串链；
* MySQL `heads` 只有一个；
* 空 MySQL 可以由 migration job 建到 head；
* Runtime 不具有 DDL 权限时仍可读取 revision；
* revision 不匹配时 fail closed。

V1 未通过不得进入 Goal 2。

## 1-B. Gate V5 — `langgraph-checkpoint-mysql==3.0.0`

在真实 MySQL 8.0.24 上测试原始第三方包。

先使用固定版本原包：

`langgraph-checkpoint-mysql[asyncmy]==3.0.0`

不要 vendor。

验证：

* `aget_tuple`
* `alist`
* `aput`
* `aput_writes`
* `adelete_thread`
* pending writes
* resume
* interrupt
* retry
* rollback
* thread branch
* long conversation
* concurrent checkpoint writes
* `checkpoint_channel_mode=full`
* asyncmy connection pool

额外核对：

### INSERT IGNORE

确认以下字段真实值不会超过 package Schema 长度：

* thread_id
* checkpoint_id
* task_id
* channel
* version

确认不存在被 `INSERT IGNORE` 静默吞掉的真实错误。

### Large Checkpoint

实测：

* blob size
* DB query memory
* read/write throughput
* `max_allowed_packet`

不得因为性能猜测提前加入 Object Storage。

### Event Loop

确认 `AsyncMySaver`：

* 在 running event loop 内创建；
* 不在 module import 阶段构造；
* 不跨不兼容的 event loop 使用。

## 1-C. 决定 Saver 引入方式

V5 通过：

使用：

`langgraph-checkpoint-mysql[asyncmy]==3.0.0`

直接固定版本依赖。

V5 出现缺口：

优先级：

1. adapter / subclass；
2. 必须修改 package internal SQL 时才 vendor；
3. 最后才考虑自研。

不得因为：

* 表名；
* COMMENT；
* `orjson`；
* JSON；
* BLOB；
* 索引命名；

而 vendor。

如果组织明确禁止直接引入该第三方 Runtime dependency，则记录正式依赖准入结论，并走 vendor fallback。

## 1-D. 确认 Production Migration Model

正式冻结：

### Application Schema

运维执行：

`alembic upgrade head`

### Checkpoint Schema

由 Saver 3.0.0 `MIGRATIONS` 导出 / 固化成独立的运维 migration artifact。

例如：

`database/mysql/checkpoint/`

生产 Runtime 不调用 `setup()`。

### Runtime

只读验证：

* Application revision；
* checkpoint 4 表存在；
* `checkpoint_migrations` version。

不满足：

* startup fail closed；
* readiness=false。

## 1-E. 固化已经完成的 Gate 结论

V3 已完成：

`SELECT MAX(seq) ... FOR UPDATE`

在 MySQL RC 下不能串行化。

实施阶段采用真实存在的 thread metadata 行作为锁锚点。

注意：

文档里的 `threads` 表名必须修正。

当前 Application Schema 中实际表是：

`threads_meta`

Goal 3 实施前必须确认并使用真实 ORM 表名 / PK。

V6 已完成：

MySQL `JSON_TYPE()` 返回大写类型。

V2 已降级：

checkpoint namespace 主键使用 hash，不再是阻塞风险。

## 验证

输出一份 Gate Result：

* V1 PASS / FAIL；
* V5 PASS / FAIL；
* Saver 最终方案；
* migration execution model；
* remaining blockers。

## 退出条件

必须同时满足：

* V1 PASS；
* V5 PASS，或者已得到明确可接受 fallback；
* 生产 Runtime 零 DDL 方案已固定；
* Saver 版本已固定；
* Checkpoint migration artifact 的生成方式已固定。

否则停止，不进入 Goal 2。

---

# Goal 2 — MySQL Persistence Foundation

## 目标

建立 MySQL 的基础运行能力：

* driver；
* config；
* connection pool；
* migration plumbing；
* Checkpoint runtime；
* Schema verification；
* migration artifacts。

本 Goal 不处理所有业务 SQL 并发细节，那些留到 Goal 3。

## 2-A. MySQL Dependencies

新增 mysql extra，至少包含：

* `asyncmy`
* `PyMySQL`
* `langgraph-checkpoint-mysql[asyncmy]==3.0.0`

版本严格固定 CheckpointSaver。

不要引入 Postgres → MySQL compatibility framework。

## 2-B. Database Config

扩展：

`database.backend`

支持：

`mysql`

扩展：

`checkpointer.type`

支持：

`mysql`

新增 / 明确：

`mysql_url`

生成：

* async SQLAlchemy URL：
  `mysql+asyncmy://`
* sync SQLAlchemy URL：
  `mysql+pymysql://`

保留 PyMySQL：

它用于 `SqlAgentStore`，不是同步 CheckpointSaver。

## 2-C. MySQL Application Engine

实现 MySQL engine 参数：

* `pool_pre_ping`
* `pool_recycle`
* READ COMMITTED isolation
* MySQL 合适的 timeout

删除 Runtime 自动数据库创建能力。

不要实现：

`CREATE DATABASE IF NOT EXISTS`

目标 database 不存在：

Runtime 启动失败。

## 2-D. Checkpoint Connection Pool

为 `AsyncMySaver` 创建真正 asyncmy pool。

不要直接依赖只提供单 connection 的 convenience API。

保持：

* 独立 Checkpointer pool；
* Application SQLAlchemy pool 与 Checkpoint pool 生命周期分离。

## 2-E. Checkpoint Runtime Integration

`async_provider.py` 新增 MySQL 分支：

* 创建 pool；
* 创建 `AsyncMySaver`；
* 不调用 `setup()`；
* 先执行 `verify_checkpoint_schema()`；
* schema 正确才 yield saver。

保持：

`checkpoint_channel_mode=full`

不要挂 Redis checkpoint cache。

不要启用同步 MySQL Saver。

## 2-F. Checkpoint Migration Artifact

从固定的 Saver 3.0.0 `MIGRATIONS` 中生成 / 冻结生产运维可执行 artifact。

要求：

* 明确版本；
* 可审查；
* 可重复执行或有明确版本推进规则；
* DBA 可独立运行；
* 不依赖 Gateway；
* 与 Runtime process 生命周期无关。

Runtime 不能执行该 artifact。

## 2-G. Application MySQL Migration Chain

建立：

`persistence/migrations_mysql/`

包含独立：

`env.py`

不要复用 PostgreSQL search_path / schema 逻辑。

此时完成 migration infrastructure。

`0001_mysql_baseline` 的最终 Schema 必须和 Goal 3 的最终 ORM 形态一致。

如果当前 Goal 2 先建立 chain plumbing，可先准备 baseline 文件框架；

**最终 baseline 内容必须在 Goal 3 的 ORM / Schema 修改完成后一次性冻结。**

不要产生：

* backfill revision；
* transitional revision；
* PG compatibility revision。

## 2-H. Runtime Schema Verification

新增只读校验：

### Application

验证：

`alembic_version == required_head`

### Checkpoint

验证：

* `checkpoint_migrations`
* `checkpoints`
* `checkpoint_blobs`
* `checkpoint_writes`
* required checkpoint migration version

任何不匹配：

* fail closed；
* readiness=false；
* 明确错误信息。

不能自动 repair。

## 2-I. Dev/Test Migration Path

开发 / CI 可以显式：

* 建 database；
* `alembic upgrade head`；
* Saver `setup()`。

但该能力必须放在 dev/test migration utility 或测试 fixture。

不得进入生产 Gateway startup。

## 验证

至少验证：

1. MySQL config 解析；
2. asyncmy engine 建立；
3. PyMySQL AgentStore 路径；
4. Checkpoint pool；
5. Checkpoint schema verification；
6. Application revision verification；
7. migration 缺失时 fail closed；
8. 无 DDL Runtime 不会尝试 repair；
9. Dev/Test 可以显式初始化。

## 退出条件

* MySQL persistence 基础设施完成；
* Runtime 已具备零 DDL 启动模型；
* Application / Checkpoint 两套 migration artifact 路径明确；
* Checkpointer 可以连接预先初始化好的 MySQL；
* 没有自动 `setup()`；
* 没有自动 Alembic migration；
* 没有自动 CREATE DATABASE。

---

# Goal 3 — Application Schema, SQL & Concurrency Migration

## 目标

完成真正的 Application Data MySQL 迁移。

本 Goal 结束后：

MySQL 已经具备完整业务运行语义。

## 3-A. Final ORM Shape

目标：

12 张 Application tables / 139 columns。

处理全部：

`DateTime(timezone=True)`

改为 MySQL：

`DATETIME(6)`

并建立统一时间边界：

`UTC aware -> naive UTC for DB`

读取时统一按 UTC 恢复。

保持：

* MySQL native JSON；
* 当前必要 FK；
* 当前 Python default；
* 已存在的 4 个真实 `server_default`。

不要：

* JSON → TEXT；
* 应用层 FK cascade；
* orphan patrol；
* 机械新增 server_default。

## 3-B. Final `0001_mysql_baseline`

现在一次性冻结：

`0001_mysql_baseline`

必须直接表达最终 Schema。

包含：

* 12 张应用表；
* 139 列；
* `DATETIME(6)`；
* 1 个真实 FK；
* 原有索引；
* MySQL 能力补偿索引；
* 2 个 active-state generated-column unique constraints。

不要包含：

* Channel tables；
* `mcp_tasks`；
* `subagent_batches`；
* Checkpoint 4 表；
* backfill；
* historical PG migrations。

OAuth identity：

直接使用：

`UNIQUE (oauth_provider, oauth_id)`

不要 generated column / CONCAT workaround。

## 3-C. Partial Unique

只处理两处：

1. 每 thread 最多一个 active run；
2. 每 scheduled task 最多一个 active occurrence。

使用 MySQL generated column + unique index。

不要为 OAuth 创建 generated column。

## 3-D. JsonMatch

给 `persistence/json_compat.py` 增加 MySQL compiler。

必须使用 MySQL `JSON_TYPE()` 的大写值：

* INTEGER
* DOUBLE
* STRING
* BOOLEAN
* NULL
* OBJECT
* ARRAY

覆盖 pinned / archived filtering and sorting。

## 3-E. Rewrite `RETURNING`

全部清除 4 处 PostgreSQL `RETURNING`：

* lease renew；
* cancel；
* finalize；
* occurrence sequence。

根据实际语义使用：

* `rowcount`
* `SELECT ... FOR UPDATE`
* `UPDATE`

不要使用：

`LAST_INSERT_ID(expr)`

## 3-F. Rewrite PostgreSQL UPSERT

`user_preferences`

从 PostgreSQL：

`ON CONFLICT`

改为 MySQL：

`ON DUPLICATE KEY UPDATE`

不要使用可能吞异常的 `INSERT IGNORE` 替代正常 upsert。

## 3-G. `run_events.seq`

按已关闭的 V3 结果实施。

不要：

`MAX(seq) FOR UPDATE`

不要 Redis sequence。

不要 GET_LOCK。

使用当前真实存在的 Thread Metadata row 作为 transaction lock anchor。

按当前 Schema，应核实为：

`threads_meta.thread_id`

在同事务：

1. `SELECT thread_id FROM threads_meta WHERE thread_id=? FOR UPDATE`
2. 获取锁后再读 `MAX(run_events.seq)`
3. 分配下一 seq
4. insert event
5. commit

必须先确认实际 ORM table / PK，不能使用文档里不存在的 `threads` 示例表。

增加并发 regression：

两个独立 transaction / worker 同时写同一 thread：

* 都成功；
* 无 1062；
* seq 连续；
* 无事件丢失。

## 3-H. Scheduler Advisory Lock

替换 Scheduler 全局 budget PG advisory lock。

使用 MySQL transaction-scoped row locking：

* 已存在可作为锁锚点的真实行优先；
* 若确实不存在，再使用最小 sentinel row。

不要 GET_LOCK。

不要 Redis。

## 3-I. READ COMMITTED

所有 MySQL Runtime connection 必须明确使用：

`READ COMMITTED`

不要依赖 MySQL 默认 RR。

验证：

* Application SQLAlchemy pool；
* Checkpoint pool；

实际 connection isolation 都正确。

## 3-J. `FOR UPDATE` / `SKIP LOCKED`

重新审计 Goal 0 后真实剩余调用。

预计：

* 28 处 `FOR UPDATE`
* 2 处 `SKIP LOCKED`

逐个确认：

* WHERE 有可用索引；
* 锁范围合理；
* 无意外全表锁。

重点：

`scheduled_task_runs`

增加真正为 queue claim 正确性需要的复合索引。

不要做其它 workload tuning。

## 3-K. MySQL Error Mapping

补 MySQL：

* 1062 duplicate；
* 1213 deadlock；

尤其修复 auth repository 的 constraint detection。

OAuth / email duplicate 必须继续转换为业务层已定义错误。

不得让 MySQL `IntegrityError` 直接向 API 冒泡变成 500。

## 3-L. Silent SQL Compatibility Scan

全仓扫描并清除：

* `RETURNING`
* `date_trunc`
* `EXTRACT(epoch)`
* PG-only SQL function
* PG-only system catalog
* raw `ON CONFLICT`
* advisory lock
* PG regex / JSON-specific syntax

注意 SQLAlchemy 能编译通过不代表 MySQL 能运行。

增加 CI guard：

对核心 ORM statement 使用 MySQL dialect 编译 / 静态扫描。

## 3-M. Multi-instance Semantics

重点重新验证：

### Scheduler

* claim；
* lease；
* occurrence；
* reconciliation；
* SKIP LOCKED。

### Run Ownership

* worker lease；
* renew；
* cancel；
* finalize；
* failover。

不再验证已经删除的：

* MCP Tasks；
* SubAgent Batches。

## 验证

真实 MySQL 8.0.24 上执行：

* ORM CRUD；
* user/auth；
* threads；
* runs；
* run events；
* agents；
* managed subagents；
* projects；
* scheduler；
* preferences；
* feedback；
* checkpoint interaction；
* duplicate constraints；
* concurrency tests。

必须验证：

* 所有 4 类静默兼容问题均被消除；
* `run_events.seq` 高并发无 1062；
* Scheduler 多实例不重复执行；
* Run lease 多实例正确；
* 时间亚秒精度保留；
* JSON filtering 正确。

## 退出条件

* Application Schema final；
* `0001_mysql_baseline` final；
* 所有 PostgreSQL-specific Application SQL 都有 MySQL 最终实现；
* 多实例 correctness tests 通过；
* 无已知 MySQL 静默语义错误。

---

# Goal 4 — Full MySQL Integration & Production-Like Acceptance

## 目标

以接近生产的方式证明：

**完全不依赖 PostgreSQL 的 Runtime 可以在真实 MySQL 8.0.24 + 无 DDL 权限应用账号下稳定工作。**

这是切换前最终门禁。

## 4-A. 准备真实 MySQL 8.0.24

使用：

* MySQL 8.0.24；
* `utf8mb4`；
* READ COMMITTED；
* 明确 `max_allowed_packet`；
* migration admin account；
* Runtime application account。

Runtime account 只授予需要的 DML 权限。

不授予 DDL。

## 4-B. 运维模拟发布

使用 migration account：

1. 创建 database；
2. 执行 Application Alembic migrations；
3. 执行 Checkpoint migration artifact；
4. 验证版本。

然后切换为 Runtime account。

## 4-C. Runtime Permission Acceptance

Runtime account 启动 Gateway。

必须成功。

同时确认：

* 没有 CREATE；
* 没有 ALTER；
* 没有 DROP；
* 没有 CREATE INDEX；
* 没有 Alembic upgrade；
* 没有 Saver setup。

可通过：

* MySQL audit/general log；
* information_schema；
* 权限拒绝测试；

进行确认。

## 4-D. Schema Missing Failure Test

分别制造：

* Application revision 缺失；
* Application revision 过旧；
* checkpoint table 缺失；
* checkpoint migration version 过旧。

Runtime 必须：

* fail closed；
* readiness=false；
* 给出明确错误；
* 不尝试自动修复。

## 4-E. End-to-End Agent

至少覆盖：

`thread create`
→ `run`
→ `SSE`
→ `checkpoint`
→ `resume`
→ `tool call`
→ `final response`

再覆盖：

* interrupt；
* retry；
* rollback；
* regenerate / branch；
* project；
* agent；
* managed subagent；
* ordinary MCP；
* ordinary SubAgent task。

## 4-F. Scheduler

真实测试：

* cron / scheduled task；
* occurrence claim；
* overlapping policy；
* lease；
* retry；
* multi-instance。

至少两个 Gateway / worker 实例。

## 4-G. Run/Event Multi-instance

至少验证：

* 同 thread 并发事件；
* run ownership；
* lease renew；
* cancel；
* worker fail/recovery；
* SSE history consistency。

## 4-H. Checkpoint Acceptance

验证：

* resume；
* interrupt；
* pending writes；
* rollback；
* concurrent writes；
* long conversation；
* large checkpoint payload。

记录：

* max blob；
* p99 blob；
* read/write latency；
* `max_allowed_packet` 余量。

只观察，不做 Object Storage 优化。

## 4-I. Fresh Cutover Strategy

开发 /测试期间可以分别验证：

* Application MySQL；
* Checkpoint MySQL。

但**生产 fresh cutover 不要求经历 PG/MySQL hybrid backend 阶段**。

生产最终一次切换到：

`Application = MySQL`
`Checkpointer = MySQL`

不要为了迁移人为维护：

`Application=PG + Checkpoint=MySQL`

或：

`Application=MySQL + Checkpoint=PG`

作为生产阶段。

## 验证总表

必须全部 PASS：

1. Gateway startup；
2. Agent chat；
3. SSE；
4. Checkpoint resume；
5. interrupt；
6. retry；
7. rollback；
8. Scheduler；
9. multi-instance；
10. MCP ordinary tools；
11. SubAgent ordinary task；
12. long conversation；
13. large payload；
14. Runtime application account 无 DDL；
15. migration 未执行时 fail closed；
16. migration 完成时 ready；
17. Runtime 全程零 DDL；
18. MySQL restart 后 durable state 正常。

## 退出条件

只有全部通过才能进入 Goal 5。

不要因为“大部分测试通过”提前删除 PostgreSQL。

---

# Goal 5 — PostgreSQL Removal & Final Cleanup

## 目标

在 MySQL 已成为唯一生产持久化后端后，彻底删除 PostgreSQL implementation。

最终仓库不保留“以后也许会切回 PG”的兼容代码。

## 5-A. 删除 PostgreSQL Driver

删除：

* asyncpg；
* psycopg；
* psycopg-pool；
* postgres optional extra；
* PG-only connection config。

保留：

* asyncmy；
* PyMySQL；
* MySQL CheckpointSaver dependency。

## 5-B. 删除 PostgreSQL Checkpointer

删除：

* AsyncPostgresSaver；
* PostgresSaver；
* Postgres pool；
* postgres checkpointer provider branch；
* PG health probe。

同步 MySQL Saver仍然不需要增加。

## 5-C. 删除 PostgreSQL Schema Helpers

删除：

* `postgres_schema.py`
* search_path
* libpq options
* CREATE SCHEMA support
* PostgreSQL schema validation。

## 5-D. 删除 PostgreSQL Migration Chain

删除旧 PG migration history：

`0001`–`0023`

只保留最终 MySQL migration chain。

删除 PG-only：

* migration env；
* revision helpers；
* PG system catalog handling。

Git history 本身已经保留历史，不需要在 active source tree 再维护。

## 5-E. 删除 Legacy Bootstrap

删除只为 PostgreSQL 历史升级服务的：

* `_CANONICAL_0019_SCHEMA_FLOOR`
* `_BASELINE_TABLE_NAMES`
* `_BASELINE_INDEX_NAMES`
* `_BASELINE_REVISION`
* `_FORWARD_COMPATIBLE_REVISION`
* `_validate_forward_schema`
* `_run_baseline_create_all_sync`
* `_postgres_lock`
* `_PG_LOCK_KEY`
* PG legacy branch
* PG forward-compatible branch
* 对应 pin tests

注意：

如果 SQLite / dev path 仍使用 `_run_create_all_sync()`，保留该通用能力。

## 5-F. 删除 PostgreSQL SQL Branches

删除所有：

`if dialect == "postgresql"`

仅保留仍实际支持的：

* mysql；
* sqlite / memory（如果仍作为 dev/test backend）。

删除：

* `pg_insert`
* `ON CONFLICT`
* advisory locks
* PG JSON compiler
* PG-specific error parsing。

`json_compat.py` 保留：

* sqlite；
* mysql；

如果两者仍需要。

## 5-G. 清理 Tests

删除：

* PG-only tests；
* PG bootstrap tests；
* PG migration tests；
* PG driver tests。

把真正 backend-neutral 的测试保留。

不要留下大规模 PG/MySQL parameterization。

最终核心集成测试使用：

MySQL 8.0.24。

## 5-H. 清理 Deployment

删除：

* Helm PostgreSQL StatefulSet；
* PG secrets；
* PG environment variables；
* PG Docker/deploy references。

不要因此新增 Kubernetes MySQL StatefulSet。

生产 MySQL 继续支持 external DSN。

Docker Compose 只保留开发所需 MySQL。

## 5-I. 清理 Documentation

更新：

* `README`
* `README-zh`
* `backend/AGENTS.md`
* `config.example.yaml`
* deployment docs
* migration docs
* Feature Inventory

明确：

* Production persistence = MySQL；
* PostgreSQL 不再是 supported backend；
* Redis 不承担 durable truth；
* production Runtime zero DDL；
* migrations 由运维执行。

## 5-J. Final Dead-code Scan

全仓扫描：

* postgres
* postgresql
* asyncpg
* psycopg
* pg_
* ON CONFLICT
* RETURNING
* advisory
* search_path
* pg_catalog
* pg_index
* to_regclass

每个剩余命中必须：

* 有明确保留理由；
* 或删除。

不得留下无法解释的 PG compatibility branch。

## 最终验收

重新从全新环境执行：

1. 创建空 MySQL 8.0.24 database；
2. 运维 migration account 执行 Application migration；
3. 运维执行 Checkpoint migration artifact；
4. 使用无 DDL Runtime account 启动系统；
5. Gateway ready；
6. 跑完整 Agent E2E；
7. 跑 Scheduler；
8. 跑 multi-instance；
9. 跑 checkpoint resume / interrupt / rollback；
10. 确认无 PostgreSQL 依赖。

## 最终退出条件

满足以下所有条件：

* PostgreSQL Runtime code = 0；
* PostgreSQL driver = 0；
* PostgreSQL active migrations = 0；
* PostgreSQL deploy dependency = 0；
* MySQL 是唯一生产关系数据库；
* production Runtime zero DDL；
* MySQL fresh bootstrap 可重复；
* Checkpoint migration 可由 DBA 独立执行；
* 全量核心测试通过；
* 文档与实现一致。

至此 PostgreSQL → MySQL fresh-cutover 完成。

---

# Non-Goals

整个迁移过程中不要顺手实现：

* Redis checkpoint cache；
* delta checkpoint；
* Redis durable lease；
* Object Storage checkpoint overflow；
* Kubernetes MySQL；
* Helm MySQL Operator；
* `mcp_tasks`；
* `subagent_batches`；
* sync MySQL CheckpointSaver；
* Store MySQL backend；
* JSON → TEXT；
* 去 FK；
* 全量 server_default；
* Schema Compliance；
* 无关索引优化；
* query tuning；
* 历史数据 migration；
* dual write；
* rollback to PostgreSQL；
* 通用 database portability framework。

这些全部属于迁移之外的独立问题。
