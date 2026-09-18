# G1 — MySQL Compatibility Gates

**推荐模型：GPT-5.6 Terra**
**推理强度：High**

你现在要执行 PostgreSQL → MySQL 8.0.24 Migration 的 **Goal 1：Compatibility Gates**。

这是一个 **Gate + focused implementation / verification Goal**，不是全面迁移 Goal。

开始前必须先阅读：

* `mysql-migration-design.md` —— **唯一权威迁移设计**
* 当前仓库 `HEAD`
* 当前 `AGENTS.md`
* 与 migration / checkpointer / persistence 相关的现有测试

如果代码与文档中的历史行号不一致，以**当前 HEAD 的真实代码结构**为准，但不得擅自改变已经冻结的设计结论。

## 已冻结结论

以下事项不要重新讨论：

* CheckpointSaver 已确定：
  `langgraph-checkpoint-mysql[asyncmy]==3.0.0`
* 默认 direct dependency，不 vendor
* V5 不是 Saver selection Gate
* Production Runtime 永远不调用 `saver.setup()`
* Production Runtime zero DDL
* Checkpoint DDL 由运维 / DBA 执行
* fresh cutover
* 不迁移 PG 历史数据
* 不 dual-read / dual-write
* 不 backfill
* 不设计 MySQL → PostgreSQL 数据回滚
* `checkpoint_channel_mode=full`
* 不引入 Redis checkpoint cache
* 不做 Checkpoint Object Storage overflow
* V3 已关闭
* V6 已关闭
* V2 已关闭
* Goal 0 已完成，不恢复已删除能力

本 Goal 只关闭：

1. **V1 — MySQL Alembic independent chain**
2. **V5 — `langgraph-checkpoint-mysql==3.0.0` compatibility / correctness**

---

## Part A — V1 Alembic Independent Chain Gate

目标：

证明 MySQL 可以使用独立 Alembic migration chain，而且不会与现有 PostgreSQL legacy chain 串联。

确认当前 PG legacy chain：

* 当前 PG head；
* `0001`–`0023` 保持 immutable；
* 本 Goal 不修改 PG historical revisions。

实现或验证 MySQL chain 的最小接线方式：

* backend 可以选择独立 MySQL `script_location`；
* MySQL 最终链根名称固定为：
  `0001_mysql_baseline`
* PG chain 与 MySQL chain 不存在 `down_revision` 关系；
* 不 replay PostgreSQL historical migrations；
* 默认继续使用 `alembic_version`；
* 不创建通用 multi-chain framework。

处理现有 bootstrap 中：

* `_HEAD_REVISION`
* `_KNOWN_REVISIONS`

按照 `mysql-migration-design.md` 的结论：

优先从生产路径移除这些无必要的模块级 revision cache，而不是引入按 chain 缓存的新 abstraction。

禁止实现：

* migration chain registry；
* per-chain class hierarchy；
* migration cache manager；
* generic database migration framework。

### V1 的验证方式

本 Goal 不要求现在就冻结最终 Application `0001_mysql_baseline` 内容。

可以使用：

* isolated Alembic fixture；
* temporary test migration tree；
* 最小 proof；

验证以下语义：

1. PostgreSQL script directory 只看到 PostgreSQL revisions；
2. MySQL script directory 只看到 MySQL revisions；
3. 两边都只有各自的 single head；
4. MySQL chain 不引用 PG revision；
5. backend 选择不会串 chain；
6. MySQL migration execution 可以由 Runtime 外部独立调用；
7. Runtime 侧可以只读判断 current revision；
8. revision mismatch 可以被识别为 startup/readiness failure 条件。

不要为了 V1 提前实现完整业务 Schema。

---

## Part B — V5 CheckpointSaver Gate

使用真实：

`MySQL 8.0.24`

验证已经冻结的：

`langgraph-checkpoint-mysql[asyncmy]==3.0.0`

这是 compatibility/correctness verification，不是选型。

测试环境允许显式调用：

`saver.setup()`

因为测试环境拥有 DDL 权限。

这不代表 Production Runtime 可以调用 `setup()`。

### 必测核心 API

真实执行：

* `aput`
* `aget_tuple`
* `alist`
* `aput_writes`
* `adelete_thread`

覆盖：

* checkpoint namespace；
* parent checkpoint；
* metadata；
* pending writes；
* pending sends（若当前 Runtime 会触达）；
* filter；
* before；
* limit；
* serde round-trip。

### 必测 DeerFlow Runtime 语义

优先复用现有 regression tests，验证：

* resume；
* interrupt；
* retry；
* rollback；
* branch / regenerate；
* pending writes；
* 多轮长会话；
* concurrent checkpoint writes；
* `checkpoint_channel_mode=full`。

不要为已经有覆盖的行为重复创建大量新测试。

### asyncmy Pool

不能只验证上游：

`AsyncMySaver.from_conn_string()`

因为它走单 connection。

需要验证：

`AsyncMySaver(conn=<asyncmy.Pool>)`

确认：

* `_ainternal.get_connection` 可以识别 pool 的 `acquire()`；
* 并发调用能从 pool 获取不同连接；
* connection / transaction lifecycle 正确；
* pool close 正确。

### Event Loop Lifecycle

确认：

* Saver 在 running event loop 内构造；
* 不在 module import 阶段构造；
* 不跨错误的 event loop 使用；
* 当前 DeerFlow async provider 的生命周期可以满足上游 `asyncio.get_running_loop()` 约束。

### INSERT IGNORE 风险

核对第三方实现涉及 `INSERT IGNORE` 的列：

* `thread_id`
* `checkpoint_id`
* `task_id`
* `channel`
* `version`

确认 DeerFlow 实际产生的值不会因为列长产生 silent truncation / ignored write。

只基于真实取值和 Schema 做结论，不做理论性无限边界测试。

### Large Checkpoint

构造具有代表性的长会话 / 大 checkpoint。

记录：

* max blob size；
* 典型 blob size；
* read/write latency；
* base64 transport amplification；
* MySQL memory / materialization 风险；
* 当前 `max_allowed_packet`；
* payload 距离 packet limit 的余量。

这里只收集证据。

禁止因此实现：

* Object Storage overflow；
* blob threshold；
* blob_ref；
* GC；
* 新 checkpoint abstraction。

---

## V5 失败处理

出现失败时，必须严格按顺序：

1. 检查是不是项目接入方式错误；
2. adapter / wrapper；
3. subclass；
4. 必须改 package internal SQL / private implementation 时才 vendor **同一个 3.0.0**；
5. 只有结构性、无法修复的 correctness 问题才升级为 architecture blocker。

不得跳级。

不得重新进行 Saver 市场选型。

不得仅因为：

* 表名；
* COMMENT；
* 索引命名；
* JSON；
* LONGBLOB；
* `orjson`

而 vendor。

---

## Testing Policy

遵守 Risk-Adjusted Verification：

* 优先复用现有 tests；
* 只增加能够证明 V1/V5 风险的 focused tests；
* 不测试 framework 自身显然保证的行为；
* 不构建 PG×MySQL 全量参数化矩阵；
* 不做 exhaustive edge-case matrix；
* 不运行与本 Goal 无关的大型测试。

真实 MySQL 验证优先级高于 mock。

---

## 本 Goal 禁止做

不要：

* 修改大批 ORM model；
* 改 29 个时间列；
* 改 RETURNING；
* 改 advisory lock；
* 改 generated column；
* 改 Scheduler；
* 接入正式生产 MySQL Runtime；
* 删除 PostgreSQL；
* vendor 第三方源码；
* 实现 Redis；
* 实现 Compliance Pass。

这些属于后续 Goal。

---

## 输出

完成后给出：

### V1

* PASS / FAIL
* 验证证据
* 是否存在 blocker

### V5

* PASS / FAIL
* 每类 Runtime semantics 的验证结果
* asyncmy pool 结果
* event-loop 结果
* large payload 结果
* `INSERT IGNORE` 结论
* 如果失败：落在 fallback ladder 第几档

### 总结

明确回答：

> 是否允许进入 Goal 2？

如果不能进入，停止，不要自行开始 Goal 2。

---

# Review R1 — G1 Gate Review

**推荐模型：GPT-5.6 Sol**
**推理强度：High**

这是一次**只读 Gate Review**。

不要实现新功能，不要重写方案，不要扩大范围。

阅读：

* `mysql-migration-design.md`
* 当前 HEAD
* G1 diff
* V1 evidence
* V5 evidence
* G1 新增/修改的测试

重点审查：

1. V1 是否真的证明 MySQL / PG migration chain 隔离；
2. 是否存在 revision/head 串链风险；
3. 是否无必要地引入了 migration abstraction；
4. V5 是否真正覆盖 DeerFlow 使用的 Checkpoint semantics；
5. resume / interrupt / retry / rollback / branch / pending writes 是否证据充分；
6. asyncmy pool 是否真正工作，而不是退化成单 connection；
7. Saver/event-loop 生命周期是否正确；
8. `INSERT IGNORE` 是否存在被忽略的真实 correctness 风险；
9. large checkpoint 是否暴露 blocker；
10. 是否有任何证据要求进入 wrapper / subclass / vendor；
11. 是否可以安全进入 Goal 2。

只报告：

### Blocking issues

### High-risk issues

### Missing evidence

### Gate verdict

不要：

* 给代码风格建议；
* 给命名建议；
* 给 Optional Compliance 建议；
* 给未来性能优化建议；
* 重复已经通过测试证明的低风险事项。

如果没有 blocker，明确写：

`G1 PASS — safe to proceed to Goal 2`

---

# G2 — MySQL Persistence Foundation

**推荐模型：GPT-5.6 Terra**
**推理强度：High**

前置条件：

* G1 V1 PASS；
* G1 V5 PASS，或已经按固定 fallback ladder 得到可接受实现；
* R1 没有 blocker。

现在实现 **MySQL Persistence Foundation**。

设计来自 `mysql-migration-design.md`，架构结论已经冻结。

不要重新设计迁移架构。

如果实现过程中发现设计与当前代码存在真实矛盾：

记录为 blocker，并说明证据。

不要通过新增通用 abstraction 自行绕开。

---

## 1. Dependencies

新增/确认 MySQL optional dependencies：

* `asyncmy>=0.2.10`
* `PyMySQL>=1.1.1`
* `langgraph-checkpoint-mysql[asyncmy]==3.0.0`

CheckpointSaver 必须精确固定：

`==3.0.0`

保留 PyMySQL：

它用于同步 `SqlAgentStore`。

不要因此启用同步 MySQL CheckpointSaver。

---

## 2. Config

扩展：

`database.backend`

支持：

`mysql`

扩展：

`checkpointer.type`

支持：

`mysql`

新增/完善：

`mysql_url`

生成：

* async SQLAlchemy URL：
  `mysql+asyncmy://`
* sync SQLAlchemy URL：
  `mysql+pymysql://`

保留现有：

* memory
* sqlite
* postgres

直到 Goal 6。

不要创建 generic dialect config hierarchy。

---

## 3. Application MySQL Engine

实现 MySQL engine 支持。

至少处理：

* asyncmy；
* pool sizing；
* pool_pre_ping；
* pool_recycle；
* timeout；
* READ COMMITTED；
* engine disposal。

明确验证实际 MySQL connection：

`@@transaction_isolation`

确实为：

`READ-COMMITTED`

不要只依赖配置对象值。

---

## 4. 删除 Runtime 自动建库

生产 Runtime 不允许：

`CREATE DATABASE`

不要把：

`_auto_create_postgres_db`

改造成：

`CREATE DATABASE IF NOT EXISTS`

MySQL database 不存在：

Runtime 必须失败。

Dev / Compose 的 database 创建交给：

* MySQL container；
* explicit migration/dev tooling；
* DBA/Migrator。

---

## 5. Async MySQL Checkpointer Integration

正式接入：

`AsyncMySaver`

使用项目自己的：

`asyncmy.Pool`

保持：

Application SQLAlchemy pool

与：

Checkpointer asyncmy pool

相互独立。

MySQL checkpointer Runtime path：

1. create pool；
2. create `AsyncMySaver(conn=pool)`；
3. verify checkpoint schema；
4. yield saver；
5. close pool。

明确：

**Production Runtime 不调用 `setup()`。**

---

## 6. Checkpoint Schema Verification

实现只读：

`verify_checkpoint_schema`

检查：

* `checkpoint_migrations`
* `checkpoints`
* `checkpoint_blobs`
* `checkpoint_writes`

并验证：

`MAX(v) == len(MIGRATIONS) - 1`

required version 从固定的 3.0.0 定义推导。

不要散落 magic number。

失败时：

* fail closed；
* readiness=false；
* 明确报错。

不得：

* 自动 setup；
* 自动 upgrade；
* 自动 repair。

---

## 7. Application Schema Verification 基础能力

实现 Runtime 只读 Application revision check 的基础能力：

* 读取 `alembic_version`；
* 确保只存在期望 revision；
* mismatch 明确失败。

本 Goal 还没有冻结最终 `0001_mysql_baseline`。

因此：

* 可以实现 verification mechanism；
* focused tests 可以使用 synthetic / test revision；
* 最终真实 baseline acceptance 留到 Goal 5。

不要为了现在通过 Gateway E2E 而提前冻结错误的 baseline。

---

## 8. MySQL Migration Infrastructure

建立 MySQL 独立 Alembic infrastructure：

例如：

`persistence/migrations_mysql/`

包括：

* env；
* script config；
* versions location；
* backend selection。

不要复制：

* postgres search_path；
* CREATE SCHEMA；
* libpq options；
* postgres_schema handling。

暂时不要把最终 business schema 锁死。

---

## 9. Checkpoint Migration Artifact

建立生产运维可执行的：

`database/mysql/checkpoint/`

或等价清晰目录。

内容必须从：

`langgraph-checkpoint-mysql==3.0.0`

的 `MIGRATIONS` 派生。

要求：

* versioned；
* linear；
* auditable；
* DBA 可以脱离 Gateway 执行；
* 与 Saver 3.0.0 明确绑定；
* checkpoint 4 tables 不进入 Alembic Application revisions。

升级 Saver 版本时必须重新 review migration artifact。

---

## 10. Dev / CI / Compose

Dev / CI 可以显式拥有 DDL 权限。

允许：

* Alembic migrate；
* Saver `setup()`；
* test fixture create/reset DB。

但这些调用路径不能被 Production Gateway startup 复用。

安全依赖**调用路径隔离**。

不要增加：

`auto_setup=true/false`

这种生产安全开关。

---

## 11. Docker / Compose

如果当前 Docker/Compose 是本项目的开发验证入口：

增加最小的：

`mysql:8.0.24`

开发服务。

避免：

* Kubernetes；
* Helm MySQL StatefulSet；
* Operator；
* 生产拓扑扩展。

支持 external MySQL DSN。

---

## 12. Health / Capability Gates

扩展现存：

* health probe；
* checkpointer backend Literal；
* database backend guards；
* `("sqlite","postgres")` 类能力守卫；

使 MySQL 不再被错误拒绝。

只改真实存在的 current HEAD 调用点。

不要根据旧文档行号机械修改。

---

## 13. Checkpoint Mode

保持：

`checkpoint_channel_mode=full`

不要挂：

`CachedHistorySaver`

不要引入 Redis。

---

## 14. Sync Path

确认：

`SqlAgentStore`

在：

`mysql+pymysql://`

下正常工作。

重点验证 graph subprocess 所需同步 Agent definition read path。

不要实现：

`PyMySQLSaver`

或其它同步 CheckpointSaver。

---

## Testing

只跑 Foundation 相关测试：

* config parsing；
* URL conversion；
* engine connect/dispose；
* READ COMMITTED；
* sync SqlAgentStore；
* asyncmy pool；
* AsyncMySaver smoke；
* schema verification；
* missing schema fail-closed；
* checkpoint migration artifact；
* migration script-location isolation；
* health/capability gates。

不要跑完整 Agent E2E。

不要跑完整 multi-instance。

这些留给 G5。

---

## 退出条件

必须达到：

* MySQL Application engine 可连接；
* sync MySQL AgentStore 可连接；
* Async MySQL Checkpointer 可连接预初始化 schema；
* Runtime Checkpointer path zero DDL；
* schema verification 可用；
* Application revision verification mechanism 可用；
* MySQL migration infrastructure 可用；
* Checkpoint migration artifact 可由 DBA 独立执行；
* dev/test 有独立 migration/setup path；
* Production Runtime 无自动建库、建表、upgrade、setup。

完成后停止。

不要开始 G3。

---

# G3 — Application Schema & SQL Compatibility

**推荐模型：GPT-5.6 Terra**
**推理强度：High**

前置：

Goal 2 PASS。

现在处理 Application ORM / SQL 的 MySQL compatibility。

这个 Goal 处理**确定性的方言与类型兼容问题**。

不要在本 Goal 做复杂 multi-instance concurrency redesign；并发锁语义留给 Goal 4。

---

## 1. 时间类型

当前有约 29 个：

`DateTime(timezone=True)`

MySQL 必须保留微秒精度：

`DATETIME(6)`

实现时必须同时兼顾过渡期仍然存在的 sqlite / postgres backend。

使用最小、清晰的 dialect-specific mapping。

不要为了这一点建立通用 Type System abstraction。

统一 DB boundary：

写入：

`UTC-aware datetime -> naive UTC`

读取：

按 UTC 恢复业务语义。

重点验证：

* microseconds 不丢；
* Scheduler FIFO；
* lease expiry；
* created_at ordering；
* scheduled_for；
* retry / timeout 时间比较。

---

## 2. JSON

保留现有 11 个：

`sa.JSON`

MySQL 使用 native JSON。

不要：

* JSON → TEXT；
* 拆 scalar columns；
* 新 serialization adapter。

---

## 3. JsonMatch

实现：

`@compiles(JsonMatch, "mysql")`

必须遵循已经实测的：

`JSON_TYPE()` 返回大写：

* `INTEGER`
* `DOUBLE`
* `STRING`
* `BOOLEAN`
* `NULL`
* `OBJECT`
* `ARRAY`

覆盖现有真实调用：

* pinned；
* archived；
* metadata filter/sort。

只为当前实际调用写测试。

---

## 4. Partial Unique

现有三个 partial unique 语义中：

只有两处需要 MySQL generated-column workaround：

### runs

每 thread 最多一个 active run：

`pending / running`

### scheduled_task_runs

每 task 最多一个 active occurrence：

`queued / launching / running`

使用设计文档确定的 generated-column + unique semantics。

不得扩展到 OAuth。

---

## 5. OAuth Identity

MySQL 直接：

`UNIQUE(oauth_provider, oauth_id)`

允许多个：

* `(NULL,NULL)`
* `('github',NULL)`
* `(NULL,'x')`

拒绝重复真实 OAuth identity。

不要：

* generated column；
* CONCAT；
* separator encoding。

---

## 6. RETURNING

清除全部 4 处生产 `RETURNING`。

逐条保持业务语义：

### renew_lease

* conditional UPDATE；
* rowcount 判断成功；
* 同 transaction 读取 `cancel_action`。

### request_cancel

* `SELECT ... FOR UPDATE`
* 根据当前值决定 first-writer-wins；
* UPDATE。

### finalize_if_not_cancelled

* conditional UPDATE；
* `rowcount == 1`。

### scheduled occurrence seq

* `SELECT last_occurrence_seq ... FOR UPDATE`
* Python +1
* UPDATE。

不要使用：

`LAST_INSERT_ID(expr)`

作为序号传递机制。

---

## 7. User Preferences UPSERT

将 PostgreSQL：

`ON CONFLICT`

替换为 MySQL：

`ON DUPLICATE KEY UPDATE`

保持逐-key patch semantics。

不要用：

`INSERT IGNORE`

代替普通业务 upsert。

---

## 8. MySQL Error Mapping

补 MySQL：

* duplicate key `1062`
* deadlock `1213`

尤其修复：

`app/gateway/auth/repositories/sqlite.py`

或当前 HEAD 中等价位置。

MySQL 1062 应通过 **key/index name** 判定具体 constraint。

OAuth duplicate 与 email duplicate 必须继续转换为现有业务错误。

不得让：

`IntegrityError`

直接冒泡成 500。

保留现有 PG/SQLite 行为直到 Goal 6。

---

## 9. Server Defaults / FK

保留现有真实：

4 个 `server_default`

不要机械给所有 Python defaults 加 DB default。

保留：

`user_preferences.user_id -> users.id ON DELETE CASCADE`

不要去 FK。

不要实现 orphan patrol。

---

## 10. SQL Compatibility Scan

全仓扫描 current production code：

* RETURNING；
* ON CONFLICT；
* date_trunc；
* EXTRACT(epoch)；
* pg_*；
* PG JSON operators；
* regex；
* PG system catalog；
* PostgreSQL-only raw SQL。

区分：

### 当前仍在 production runtime 的 PG-only SQL

需要后续迁移或 Goal 4 处理。

### legacy PG migration files

暂时保留到 Goal 6。

不要因为 legacy migration 命中就改历史 revision。

---

## 11. CI Guard

增加最小静态 / compile guard，防止 MySQL dialect 静默接受：

* RETURNING；
* date_trunc；
* EXTRACT(epoch)

这类实际服务端不支持的语句。

不要试图构建完整 SQL compatibility framework。

---

## Testing

focused tests：

* DateTime(6) precision；
* UTC boundary；
* JsonMatch；
* active run uniqueness；
* scheduled active uniqueness；
* OAuth NULL uniqueness；
* user preference upsert；
* renew/cancel/finalize；
* occurrence sequence；
* auth duplicate mapping；
* representative MySQL CRUD。

至少关键行为需要真实 MySQL 8.0.24 验证。

不要跑完整 multi-instance suite。

---

## 退出条件

* 29 个时间列语义安全；
* JSON 保持 native；
* JsonMatch 正确；
* 4 RETURNING 清除；
* 1 ON CONFLICT 清除；
* 2 partial-unique workaround 正确；
* OAuth unique 正确；
* 1062 contract 正确；
* 当前 Application SQL 不存在已知 MySQL silent incompatibility；
* PG legacy chain 未被破坏。

完成后停止。

不要开始 Goal 4。

---

# G4 — Concurrency, Locking & Multi-instance Semantics

**推荐模型：GPT-5.6 Terra**
**推理强度：High**

这是本次 Application migration 中风险最高的实现 Goal。

架构方案已经在 `mysql-migration-design.md` 中确定。

先按已确定方案实现。

不要重新研究 V3。

不要重新发明锁机制。

---

## 1. Isolation

确保 MySQL Application connections 真实工作在：

`READ COMMITTED`

使用真实连接验证：

`@@transaction_isolation`

不得依赖 MySQL 默认：

`REPEATABLE READ`

---

## 2. run_events.seq

V3 已经关闭。

已证明以下方案错误：

* `MAX(seq) ... FOR UPDATE`
* `INSERT ... SELECT MAX(seq)+1`
* 只依赖 unique constraint
* Redis sequence
* GET_LOCK

实现已经确定的方案：

### MySQL 路径

同一个 transaction 内：

1. 确保 `threads_meta` anchor row 存在；
2. 创建必须是幂等的；
3. 不得覆盖已存在 metadata；
4. `SELECT thread_id FROM threads_meta ... FOR UPDATE`；
5. 获取行锁之后再查询该 thread 的 max seq；
6. 计算 next seq；
7. insert run_event；
8. commit。

真实表：

`threads_meta`

真实 PK：

`thread_id`

禁止使用不存在的：

`threads.id`

---

## 3. Anchor Row Race

特别验证：

线程 metadata 创建失败、超时或未提前完成时：

事件路径仍然能够自己保证 anchor row 存在。

并发两个 writer 首次写同一个全新 thread 时：

* 不重复创建错误；
* 不覆盖 metadata；
* 不产生 seq collision；
* 不产生 lost update。

---

## 4. Lock Ordering

检查：

event path 对 `threads_meta` 的新锁

与现存：

* thread create；
* owner update；
* project update；
* archive/pin；
* run admission；

之间是否形成 lock-order inversion。

只处理有真实可达路径的死锁风险。

不要穷举理论状态空间。

---

## 5. Scheduler Global Budget Lock

替换：

PostgreSQL transaction advisory lock。

目标必须是：

transaction-scoped correctness。

禁止：

* GET_LOCK；
* Redis；
* process-local asyncio lock。

优先：

已有持久行作为锁锚点。

如果没有安全、语义明确的现存锚点：

采用最小 sentinel-row 方案。

不要为了锁创建通用 distributed lock abstraction。

必须证明：

多个 Gateway/Worker 下 global budget 不被超卖。

---

## 6. `FOR UPDATE` Audit

基于 current HEAD 重新扫描实际数量。

设计基线约：

28 处。

逐处确认：

* WHERE predicate；
* 是否使用 PK / unique / 有效索引；
* 是否可能退化成大量扫描锁；
* lock order；
* transaction boundary。

不要因为审计顺手做 query tuning。

---

## 7. `SKIP LOCKED`

剩余设计基线约：

2 处。

验证 Scheduler due-task claim：

* 一个 worker 锁住后；
* 第二个 worker 能跳过；
* 不重复 claim；
* 不长时间等待同一行。

---

## 8. Scheduler Claim Index

`scheduled_task_runs` occurrence queue claim 需要 correctness-level composite index。

基于真实查询：

* WHERE
* ORDER BY
* lock behaviour

确定最小正确索引。

不要添加“也许以后有用”的索引。

---

## 9. Run Ownership / Lease

真实 MySQL 下验证：

* try_start；
* renew lease；
* cancel；
* finalize；
* owner fail；
* lease expiry；
* worker takeover。

关注：

* lost update；
* duplicate owner；
* stale lease；
* 1213；
* lock wait。

---

## 10. MySQL Deadlock 1213

不要尝试“彻底消灭数据库出现 1213”这种不现实目标。

需要确认：

* 当前核心 transaction lock order 尽量一致；
* 1213 不被错误识别成业务唯一冲突；
* 可恢复路径有明确行为；
* 不产生 silent data loss。

只有存在真实可复现风险时才添加 bounded retry。

不要全局套一层盲目 DB retry middleware。

---

## 11. Concurrency Tests

必须使用真实 MySQL 8.0.24。

至少覆盖：

### run_events

两个独立 connection / worker 同时写同一 thread：

预期：

* 两个都成功；
* 无 1062；
* seq 唯一；
* seq 连续；
* 无 event loss。

### empty/new thread

两个 writer 同时首次写：

同样满足以上结果。

### Scheduler

两个 worker 同时 claim：

* 无 duplicate launch；
* budget 不超限；
* SKIP LOCKED 正确。

### Runs

两个 worker 同时：

* claim；
* renew；
* cancel/finalize；

保持现有业务 contract。

---

## Testing Policy

本 Goal 只跑：

* concurrency tests；
* lock tests；
* Scheduler focused tests；
* run ownership focused tests；
* event store focused tests。

不要再次跑：

* V5 大 payload；
* 全 Agent E2E；
* 全套 application tests。

完整系统验收在 Goal 5。

---

## 本 Goal 不做

* 不冻结最终 production acceptance；
* 不删除 PostgreSQL；
* 不做 Redis；
* 不做 performance tuning；
* 不做 Compliance Pass；
* 不做 speculative index cleanup；
* 不改 CheckpointSaver 选型。

---

## 输出

给出：

* 实际 `FOR UPDATE` audit 结果；
* 实际 `SKIP LOCKED` 结果；
* run_events concurrency evidence；
* Scheduler concurrency evidence；
* Run lease evidence；
* 1213 处理结论；
* 新增 correctness index；
* 是否存在尚未解决的并发 blocker。

若存在 blocker：

停止。

不要进入 Goal 5。

---

# Review R2 — Concurrency Correctness Review

**推荐模型：GPT-5.6 Sol**
**推理强度：High**

这是一次只读 correctness review。

阅读：

* `mysql-migration-design.md`
* 当前 HEAD
* Goal 4 diff
* concurrency tests
* lock traces / error logs
* transaction implementations

只关注：

### Lost update

是否存在两个 transaction 都成功但覆盖结果。

### Lock anchor

`threads_meta` anchor：

* 是否保证存在；
* upsert 是否真正 no-op；
* 是否可能覆盖业务字段。

### Lock ordering

是否存在：

A：threads_meta → runs

B：runs → threads_meta

这类可达 lock-order inversion。

### Transaction boundary

是否存在：

锁在 transaction A 获取，

但 read/update 在 transaction B 执行。

### run_events seq

是否仍存在：

* duplicate；
* gap due to failed transaction；
* batch rollback event loss；
* no-row lock hole。

### Scheduler

是否真正防止：

* duplicate claim；
* budget oversubscription；
* double launch。

### FOR UPDATE

是否有无索引导致过宽锁范围。

### READ COMMITTED

是否实际生效。

### 1213

是否存在明显可避免的 deadlock loop，或错误的 retry behaviour。

只输出：

1. Blocking correctness issues
2. High-risk concurrency issues
3. Missing concurrency evidence
4. Review verdict

不要输出：

* 代码风格问题；
* 性能微优化；
* schema 命名；
* COMMENT；
* Redis 建议；
* Optional Compliance。

如果无 blocker：

`G4 concurrency semantics approved — safe to proceed to Goal 5`

---

# G5 — Final MySQL Baseline & Full Production-like Acceptance

**推荐模型：GPT-5.6 Terra**
**推理强度：High**

前置：

* G1 PASS
* G2 PASS
* G3 PASS
* G4 PASS
* R2 没有 blocker

现在执行唯一一次**完整 MySQL production-like acceptance**。

本 Goal 同时冻结最终：

`0001_mysql_baseline`

---

## 1. Freeze Final ORM / Schema

先确认当前实际 ORM：

目标核心范围仍为：

12 application tables。

G0 已删除对象不得重新出现。

重新反射 current metadata。

不要盲信旧统计。

如果 business columns 与设计基线 139 有差异：

先解释差异。

只有明确属于 G2/G3/G4 的设计变化才允许接受。

---

## 2. `0001_mysql_baseline`

现在才正式冻结：

`0001_mysql_baseline`

这是 fresh-cutover root revision。

必须直接生成最终 MySQL Application Schema。

包含：

* 12 application tables；
* 当前最终 business columns；
* MySQL `DATETIME(6)`；
* native JSON；
* 1 个现有 FK；
* 现有必要 indexes；
* 2 个 active-state generated-column uniqueness workaround；
* OAuth full unique；
* Scheduler correctness composite index；
* 其它 G3/G4 确认必须存在的 correctness schema。

Checkpoint 4 tables：

**不得进入该 revision。**

它们由：

`database/mysql/checkpoint/`

独立迁移。

不要包含：

* Channel；
* GitHub webhook；
* mcp_tasks；
* subagent_batches；
* LangGraph Store；
* PG history；
* transitional schema；
* backfill；
* dual-write；
* Optional Compliance。

---

## 3. Fresh Migration Acceptance

从真正空的 MySQL 8.0.24 database 开始。

使用：

**Migration account**

执行：

1. create database；
2. Application Alembic migration；
3. Checkpoint migration artifact；
4. revision/version verification。

验证：

* MySQL chain single head；
* `0001_mysql_baseline` 可以从空库一次到最终状态；
* 不依赖 PG revision；
* 不包含 PG-specific SQL；
* checkpoint 4 tables 独立存在；
* Application / Checkpoint version 都正确。

---

## 4. Production Runtime Account

创建/使用只拥有：

* SELECT
* INSERT
* UPDATE
* DELETE

的 Runtime account。

明确不授予：

* CREATE
* ALTER
* DROP
* INDEX
* REFERENCES / DDL related privileges

使用该账号启动 Gateway。

成功启动本身就是 zero-DDL 的核心证明之一。

---

## 5. Missing Migration Negative Tests

分别验证：

### Application revision 缺失

预期：

* fail closed；
* readiness=false；
* 不自动 migrate。

### Application revision outdated

同上。

### Checkpoint table 缺失

同上。

### checkpoint_migrations outdated

同上。

### Database 不存在

startup failure。

禁止 Runtime：

* create database；
* create table；
* setup；
* alembic upgrade；
* stamp；
* create_all；
* auto repair。

---

## 6. Single-instance E2E

使用真实 MySQL。

覆盖核心生产链：

* auth/basic user；
* thread create；
* run create；
* SSE；
* messages；
* run event history；
* checkpoint write；
* next-turn resume；
* interrupt/resume；
* retry；
* rollback；
* regenerate/branch；
* project；
* agent CRUD；
* managed subagent；
* ordinary SubAgent `task`；
* ordinary MCP；
* preferences；
* feedback；
* scheduler basic run。

只验证当前真实产品能力。

不要恢复 G0 删除能力。

---

## 7. Multi-instance

至少：

2 个 Gateway / Worker instance。

验证：

* same thread active-run uniqueness；
* Run ownership；
* lease；
* failover；
* cancel；
* finalize；
* run_events seq；
* SSE history；
* Scheduler claim；
* SKIP LOCKED；
* occurrence sequence；
* global budget；
* overlap policy；
* retry/reconciliation。

---

## 8. Checkpoint Final Acceptance

验证：

* pending writes；
* resume；
* interrupt；
* retry；
* rollback；
* branch；
* concurrent checkpoint writes；
* long conversation；
* large payload；
* Runtime restart；
* MySQL restart；
* durable recovery。

记录：

* max blob；
* representative p99 / high percentile blob；
* read/write latency；
* max_allowed_packet；
* margin。

如果数据健康：

不实现 Object Storage overflow。

---

## 9. Runtime DDL Audit

确认应用完整生命周期：

DDL = 0。

优先通过：

* DML-only account；
* database audit/general log（若测试环境方便）；
* information_schema；

证明。

不要为了测试专门搭复杂审计平台。

---

## 10. Test Strategy

这是整个迁移**唯一一次完整大验收**。

在本 Goal 才执行：

* full MySQL integration；
* multi-instance；
* production permission model；
* major checkpoint regression；
* Scheduler integration。

如果已有测试已经覆盖，不重复创建第二套测试。

测试失败：

先定位真实失败层：

* migration；
* ORM；
* pool；
* CheckpointSaver；
* locking；
* Runtime；

不要通过增加 sleep/retry 隐藏 race。

---

## 11. Baseline Freeze Rule

G5 PASS 后：

`0001_mysql_baseline`

视为冻结。

后续新的业务 Schema 变化：

必须新增：

`0002+`

不得继续编辑 `0001_mysql_baseline`。

---

## 退出条件

只有全部满足才允许进入 Goal 6：

* fresh MySQL migration PASS；
* Application revision correct；
* Checkpoint version correct；
* DML-only Runtime startup PASS；
* missing migration fail closed PASS；
* Runtime zero DDL；
* Agent E2E PASS；
* Scheduler PASS；
* multi-instance PASS；
* Checkpoint resume/interrupt/retry/rollback PASS；
* long conversation PASS；
* restart durability PASS；
* 无已知 correctness blocker。

如果存在 blocker：

停止。

不要删除 PostgreSQL。

---

# G6 — PostgreSQL Removal & Final Cleanup

**推荐模型：GPT-5.6 Terra**
**推理强度：Medium**

如果仓库耦合比预期复杂，可以升 High。

前置：

Goal 5 全部 PASS。

现在 MySQL 已经成为唯一 production relational backend。

本 Goal 是 **removal / cleanup Goal**。

不是架构重设计 Goal。

不要添加替代 abstraction。

---

## 1. Remove PostgreSQL Dependencies

删除：

* asyncpg；
* psycopg；
* psycopg-pool；
* langgraph-checkpoint-postgres；
* postgres optional extra；

以及不再使用的 PG dependency glue。

保持：

* asyncmy；
* PyMySQL；
* langgraph-checkpoint-mysql 3.0.0。

---

## 2. Remove PostgreSQL Runtime

删除：

* AsyncPostgresSaver；
* PostgresSaver；
* postgres pool；
* postgres checkpointer provider branches；
* PostgreSQL health probe；
* PG engine branches；
* PG-only error handling；
* PG schema support。

同步 MySQL CheckpointSaver仍然不要增加。

---

## 3. Remove PostgreSQL Schema Helpers

删除：

* postgres_schema helpers；
* CREATE SCHEMA；
* search_path；
* libpq options；
* schema validation only used by PG。

不要碰 SQLite 自己需要的 helper。

---

## 4. Remove PG Migration Legacy

现在可以删除旧 PostgreSQL：

`0001`–`0023`

legacy migration chain。

同时删除仅服务旧 PG migration 的：

* migration env；
* revision helper；
* forward-compatible bootstrap；
* PG schema floor；
* old baseline constants；
* PG historical upgrade tests。

Git history 已经保留历史。

active source tree 不需要再留一份“以防万一”。

---

## 5. Remove PG Bootstrap

删除：

* PG session advisory bootstrap lock；
* PG-specific bootstrap state；
* `_auto_create_postgres_db` 残留；
* PG create_all/stamp compatibility；
* old forward-schema checks。

最终 Runtime 只保留：

MySQL production read-only schema verification

以及：

明确的 dev/test migration path。

---

## 6. Remove PG SQL Branches

扫描：

`dialect == "postgresql"`

逐个判断。

删除已经没有消费者的：

* pg_insert；
* ON CONFLICT；
* advisory lock；
* PG JsonMatch compiler；
* pg system catalog；
* search_path；
* PG SQL functions。

保留：

* mysql；
* sqlite；

如果 SQLite / memory 仍是支持的 dev/test backend。

不要为了“以后可能重新支持 PostgreSQL”留 dead branch。

---

## 7. Remove PG Tests

删除：

* PG-only driver tests；
* PG bootstrap tests；
* PG migration tests；
* PG schema tests；
* 已不存在实现对应的 PG-only fixture。

backend-neutral behaviour tests 必须保留。

不要删除只是因为测试名字里出现 postgres 就删除：

先确认它是否还在验证 backend-neutral contract。

---

## 8. Deployment Cleanup

删除：

* Helm PostgreSQL StatefulSet；
* PostgreSQL values；
* PG secrets；
* PG env；
* PG deployment docs；
* old PG connection examples。

不要新增：

* Kubernetes MySQL StatefulSet；
* MySQL Operator。

生产仍支持：

external MySQL DSN。

---

## 9. Config / Documentation

更新：

* README
* README-zh
* AGENTS
* config.example.yaml
* Docker docs
* deployment docs
* MySQL migration docs

明确：

Production relational DB = MySQL.

PostgreSQL no longer supported.

Production Runtime zero DDL.

Application Schema 与 Checkpoint Schema 均由运维迁移。

---

## 10. Final Dead-code Scan

全仓扫描：

* postgres
* postgresql
* asyncpg
* psycopg
* pg_
* pg_catalog
* pg_index
* to_regclass
* advisory
* search_path
* ON CONFLICT
* RETURNING

每一个剩余命中：

要么：

* 有明确合理原因；

要么：

* 删除。

不要机械删除文档中的历史说明，如果它明确属于 migration archive / historical note。

---

## 11. Verification

本 Goal 不需要重新完整重复 Goal 5 的所有压力测试。

按照 Risk-Adjusted Verification：

先跑：

* import/startup；
* config；
* migration head；
* MySQL smoke；
* Agent smoke；
* Checkpoint resume smoke；
* Scheduler smoke；
* relevant focused regression。

如果 G6 删除触及了核心 Runtime execution path：

再扩大到相应 G5 regression。

只有发现真实风险时才重新跑 full suite。

---

## 最终退出条件

最终仓库满足：

* PostgreSQL runtime code = 0；
* PostgreSQL drivers = 0；
* PostgreSQL active migration chain = 0；
* PostgreSQL deployment dependency = 0；
* MySQL 是唯一 production relational backend；
* Application MySQL migration single head；
* Checkpoint MySQL artifact 独立；
* Production Runtime zero DDL；
* SQLite / memory dev path（如仍支持）正常；
* ordinary MCP 正常；
* ordinary SubAgent 正常；
* Scheduler 正常；
* Checkpoint 正常；
* 文档与实现一致。

最后输出：

### Removed

### Intentionally retained

### Tests run

### Remaining known risks

### Final migration status

如果没有 remaining correctness blocker：

明确写：

`PostgreSQL → MySQL 8.0.24 fresh-cutover implementation complete.`
