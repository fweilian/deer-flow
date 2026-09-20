# PostgreSQL → MySQL 8.0.24 迁移设计

> **文档类型**：只读代码调查 + Schema 分析 + 迁移设计（**权威口径**）
> **调查基线**：`deer-flow` 仓库 HEAD = **`a55e5734`**（`feat_portal` 分支）
> **目标数据库**：**MySQL 8.0.24** + InnoDB
> **结论档位**：目标 **`Moderate`**（条件：Gate V5 通过）；**V5 通过前按 `Feasible with significant changes` 计**
>
> 本文只保留**当前设计与结论**，不记录设计演进过程。
> 全部数字均在上表 HEAD 上由 ORM 元数据反射 + 全仓扫描重新统计（§16 证据清单）。
>
> **相关文档**：
> [`mysql-goals.md`](./mysql-goals.md)（执行计划）、[`mysql-goal0-audit.md`](./mysql-goal0-audit.md)（G0 审计）、
> [`mysql-migration-plan.md`](./mysql-migration-plan.md)（**过程留档**，含多轮修订的对照与取舍记录，不作为实施依据）。

---

## 0. 冻结结论

> 本次 MySQL Migration 的 CheckpointSaver **已确定使用**
> `langgraph-checkpoint-mysql[asyncmy]==3.0.0`。
>
> **默认采用精确版本依赖，不 vendor。**
>
> **V5 用于验证该固定实现与 DeerFlow 当前 Runtime 的兼容性和正确性，
> 不再承担 CheckpointSaver 选型职责。**
>
> 若 V5 发现必须修改上游内部实现的真实缺口，
> 才将**同一个 3.0.0 版本** vendor 后做最小 patch。
>
> **Production Runtime 永远不调用 `setup()`，
> Checkpoint DDL 由运维 / DBA 独立执行。**

**冻结项与"仍待验证项"的边界**（本文件最重要的一条区分）：

| | 内容 | 状态 |
| --- | --- | --- |
| **已冻结** | 用哪个 CheckpointSaver | `langgraph-checkpoint-mysql==3.0.0`，**已定** |
| | 直接依赖还是 vendor | **默认直接依赖**；vendor 仅为同版本 patch fallback，**已定** |
| | 是否接受第三方 Saver | **接受**（MIT / 纯 Python / MySQL ≥ 8.0.19），**已定** |
| | 生产是否调用 `saver.setup()` | **永不调用**，**已定** |
| | 缺口出现时的处置顺序 | 接入方式 → adapter/wrapper → subclass → vendor+patch → architecture blocker，**已定**（§7.3） |
| **仍需验证（V5）** | 该固定版本在真实 MySQL 8.0.24 + 当前 Runtime 下的运行语义 | **未执行** —— 可能通过，也可能发现真实缺口 |
| | 由此决定的档位 | V5 通过 ⇒ `Moderate`；走到第 ⑤ 档 ⇒ 回落 `Feasible with significant changes` |

> ⚠️ **V5 不是"肯定通过"**。它仍是 **G2 之前的阻塞 Gate**。
> 但它**不是选型 Gate** —— 无论结果如何，实现基线都是同一个 3.0.0。

**依赖清单**（`pyproject.toml` 的 `mysql` extra）：

```toml
mysql = [
    "asyncmy>=0.2.10",                              # 异步 Application / Checkpointer 路径
    "PyMySQL>=1.1.1",                               # 同步 SqlAgentStore 路径（必须保留）
    "langgraph-checkpoint-mysql[asyncmy]==3.0.0",   # 🔴 精确固定；Checkpoint DDL 基线由它决定
]
```

> ⛔ **不启用同步 MySQL CheckpointSaver**（§7.7）。
> ⛔ **不复制约 1200 行第三方源码进仓库**。

**决策优先级**（贯穿全文）：

```
删除不需要的能力  >  复用成熟实现  >  使用 MySQL 原生能力  >  小范围适配  >  最后才新增自研 abstraction
```

**🔴 生产硬约束**：

> **生产 Application DB 账号无 DDL 权限**（只有 `SELECT` / `INSERT` / `UPDATE` / `DELETE`）。
> `CREATE DATABASE` / `CREATE TABLE` / `ALTER TABLE` / `DROP TABLE` / `CREATE INDEX` 与一切 Schema 迁移，
> 一律由运维 / DBA 用**独立的高权限迁移账号**执行。
> ⇒ 生产 Runtime 的启动路径中**不允许出现任何 DDL**，
> 该约束同时适用于 **Application Alembic 迁移**与 **Checkpoint Schema 迁移**（§11）。

---

## 1. 概述

### 1.1 总体判定

| 项 | 结论 |
| --- | --- |
| **目标档位** | **`Moderate`** —— 一次"需要仔细做的后端移植" |
| **生效条件** | 🔴 **Gate V5 通过** —— 对**已确定的** `langgraph-checkpoint-mysql==3.0.0` 做**兼容性 / 正确性验收**（§7.2 / §13） |
| **V5 未做前的已证实档位** | `Feasible with significant changes` |
| **V5 失败时的处置** | 按 §7.3 的**固定阶梯**：① 接入方式 → ② adapter / wrapper → ③ subclass 覆写 → ④ vendor 3.0.0 + 最小 patch → ⑤ architecture blocker。⛔ **不重新开启选型讨论** |
| **性质** | **MySQL fresh-cutover / PostgreSQL backend replacement**（不迁移 PG 历史数据） |
| **🔴 生产 DDL 权限模型** | **生产 Runtime 零 DDL**；Application Schema 与 Checkpoint Schema 各自有独立迁移产物，由运维 / DBA 执行（§11） |
| **🔴 Runtime 与 Schema 的关系** | **只验证，不创建、不修改、不升级**；缺失或不匹配时 **fail closed / `readiness=false`**（§11.5） |

### 1.2 关键数量与范围

| 维度 | 当前值 | 备注 |
| --- | --- | --- |
| **应用表** | **12 张 / 139 列** | ORM 反射实测（§2.2） |
| **框架表** | **5 张** | `alembic_version` + LangGraph checkpoint 4 张 |
| 索引 | **31** | |
| 库层 FK | **1** | `user_preferences.user_id → users.id` |
| `RETURNING`（生产） | **4 处** | 必须改写 |
| 事务级 advisory lock | **2 处** | 必须替换 |
| partial unique index | **3 处**（**2 处需生成列**） | OAuth 那处实测语义等价 |
| `with_for_update` | **28 处** | 需索引核验 |
| `SKIP LOCKED` | **2 处** | |
| `DateTime(timezone=True)` | **29 列** | 必须显式 `DATETIME(6)` |
| `sa.JSON` | **11 列** | 保持原生 `JSON`，不改写 |
| 数据库能力闸门 | **6 处** | 不扩展即拒绝 MySQL 启动 |
| 需扩展的 `Literal` | **2 处** | 只造成校验失败 |
| 新增第三方依赖 | **1 个包** + 2 个驱动 | `langgraph-checkpoint-mysql` / `asyncmy` / `PyMySQL` |

### 1.3 分职责域难度

| 职责域 | 难度 | 说明 |
| --- | --- | --- |
| 数据库驱动替换 | **Easy** | `asyncpg` → `asyncmy`；`psycopg` → `PyMySQL` |
| 应用 ORM / 查询兼容 | **Moderate** | 4 处 `RETURNING`、2 处 advisory lock、3 处 partial unique（2 处需生成列）、1 个 JSON 方言文件 |
| **Checkpoint 持久化** | **Moderate** | 固定版本依赖 + 连接池接入 + V5 兼容性/正确性验收 |
| 并发语义 | **Moderate–Hard** | 28 处 `FOR UPDATE` 索引核验、`run_events.seq` 串行化（§8.6）、RR→RC |
| 迁移链 / bootstrap | **Easy–Moderate** | 独立链 + 单 revision；Runtime 侧只留校验，DDL 全部移出 |
| **生产迁移执行模型** | **Moderate** | 两份迁移产物 + 两类执行方 + Runtime 只读校验（§11） |
| 切流与回退 | **Easy** | 单向切流，不设计数据层回滚 |
| **企业 Schema 规范** | **Easy（且可延后）** | 纯风格，不影响运行正确性（§2.4）；不进入正式 Goal |
| **上游代码的长期维护** | **Easy** | 精确版本依赖 ⇒ 零本地 fork；只有 V5 出"必须改上游内部实现"的缺口才降级 vendor（§7.4） |

### 1.4 结论退化条件

出现以下任一条件时，档位回到 `Feasible with significant changes`：

1. **V5 发现结构性、无法修复的 correctness 问题** —— 已冻结的 `langgraph-checkpoint-mysql==3.0.0`
   存在既不能通过**接入方式调整**、也不能通过 **adapter / wrapper / subclass** 绕开，
   且 **vendor + 最小 patch 也无法消除**的运行语义或 correctness 缺口（§7.3 第 ⑤ 档）。
   ⚠️ 单纯的"V5 失败"**不构成**退化条件 —— 先走前四档；
2. ⚠️ 组织要求 **MySQL 必须复用 PostgreSQL 的 Alembic revision 编号体系**
   （即不允许独立 chain）—— 这会重新引入 multiple-heads 问题。

---

## 2. 迁移定性、范围与边界

### 2.1 定性：MySQL fresh-cutover / PostgreSQL backend replacement

| 问题 | 答案 | 依据 |
| --- | --- | --- |
| 迁移 PostgreSQL 历史数据吗？ | ❌ **不迁移** | 空库 fresh cutover |
| 需要 dual-read / dual-write 吗？ | ❌ **不需要** | 不做双写，不做回填 |
| 需要 backfill 吗？ | ❌ **不需要** | MySQL 从空库建最终 Schema |
| 支持 PG 原地升级到 MySQL 吗？ | ❌ **不支持** | 无 in-place upgrade 路径 |
| 能"回滚到 PostgreSQL"吗？ | ❌ **不设计这条路径** | 切流单向：MySQL 上线后 PG backend 直接废弃 |
| 切换后还能读旧 PG 库吗？ | ❌ 不能 | PG 只是**尚未切走的后端**，不是数据回滚路径 |

**"不设计回滚"的具体含义**：不实现"MySQL 写失败 → 回退写 PG"的逻辑、
不保留 PG 连接池作为 fallback、不写双向数据同步。
若 MySQL 上线后发现致命问题，**处置手段是修复 MySQL，而不是切回 PG**。

### 2.2 量化口径（权威）

> 🔴 **基线 = 当前 HEAD `a55e5734`**（Goal 0 已完成）。全部数字由 ORM 元数据反射 + 全仓扫描统计。

| 维度 | 数值 | 复核方式 |
| --- | --- | --- |
| **应用表** | **12 张** | `Base.metadata` 反射 |
| **应用表列数** | **139 列** | 同上 |
| **框架表** | **5 张**（`alembic_version` + LangGraph 4 张 checkpoint 表） | 运行时创建，不属应用 ORM |
| **表总计** | **17 张** | 12 + 5 |
| `sa.JSON` 列 | **11** | 反射 |
| `DateTime(timezone=True)` 列 | **29** | 反射 |
| 库层 FK | **1**（`user_preferences.user_id → users.id`） | 反射 |
| Partial unique index | **3** | 扫描（**只有 2 处需生成列**，§4.4） |
| 索引（`ix_`/`idx_`/`uq_`） | **31** | 反射 |
| `RETURNING`（生产代码） | **4** | 扫描（`run/sql.py` 3 + `scheduled_task_runs/sql.py` 1） |
| `ON CONFLICT`（生产代码） | **1** | 扫描（`user/preferences.py`） |
| 事务级 advisory lock | **2** | 扫描（`scheduled_task_runs/sql.py` 1 + `runtime/events/store/db.py` 1） |
| **`with_for_update`** | **28** | 扫描（按**含该关键字的行数**计，含 `with_for_update=True` 关键字参数写法） |
| **`SKIP LOCKED`** | **2** | 扫描（`scheduled_tasks/sql.py:332,530`） |
| 命中 `postgres` 的 `.py` 文件 | **91**（`packages/harness/deerflow` 39 + `app/` 5 + `tests/` 47） | 扫描；**生产代码 44 个文件** |
| 数据库能力闸门（拒绝 MySQL 启动） | **6** | 扫描，§12 Goal 4（3 处 `SystemExit` + 2 处 `ValueError` + 1 处健康探针） |
| 需扩展的 `Literal` 类型定义 | **2**（`database_config.py:143`、`checkpointer_config.py:9`） | 扫描 |

**逐表列数（12 张应用表）**：
`runs` 31、`scheduled_tasks` 23、`scheduled_task_runs` 16、`threads_meta` 10、`run_events` 10、
`users` 9、`personal_access_tokens` 9、`feedback` 8、`projects` 8、`agents` 7、
`managed_subagents` 5、`user_preferences` 3 → **合计 139**

**G0 的范围收缩效果**（当前 139 列口径的由来）：

| 维度 | G0 前 | 当前 | 变化 |
| --- | --- | --- | --- |
| 应用表 | 15 | **12** | −3 |
| 应用表列数 | 224 | **139** | **−85（−38%）** |
| `sa.JSON` 列 | 20 | **11** | −9 |
| `DateTime(timezone=True)` 列 | 48 | **29** | −19 |
| 索引 | 47 | **31** | −16（−34%） |
| 库层 FK | 2 | **1** | −1 |
| **`with_for_update`** | 51 | **28** | **−23（−45%）** |
| **`SKIP LOCKED`** | 9 | **2** | **−7（−78%）** |
| 能力闸门 | 7 | **6** | −1 |
| 命中 `postgres` 的 `.py` 文件 | 113 | **91** | −22 |
| 随模块删除的代码 | — | **3795 行 / 21 个文件** | — |
| 随模块删除的测试 | — | **20 个文件** | — |

> **被删三张表的实测数据**（供核对）：
> `mcp_tasks` 45 列 / 9 索引 / 5 JSON / 10 时间列；
> `subagent_batch_items` 23 列 / 3 索引 / 3 JSON / 6 时间列；
> `subagent_batches` 17 列 / 4 索引 / 1 JSON / 3 时间列。
>
> ⚠️ **历史 PG 迁移文件仍留在仓库**（`persistence/migrations/versions/0011_mcp_tasks.py`、
> `0016_subagent_batches.py`）：它们属于**不可变的 PG 历史链**，只作审计用，**不由 Gateway 回放**。
> **不要因为看到这两个文件就以为模块还在。**

### 2.3 Runtime 范围（G0 之后生效）

| 能力 | 当前状态 | 备注 |
| --- | --- | --- |
| Channel / GitHub Webhook | 🔴 **已删除** | `app/channels/`、`gateway/github/`、3 个路由、2 个持久化包 |
| `mcp_tasks`（MCP 后台长任务） | 🔴 **已删除** | 含 `persistence/mcp_tasks`、`mcp/tasks/`、`app/mcp_tasks/`、路由与后台任务工具 |
| `subagent_batches` / `subagent_batch_items` | 🔴 **已删除** | 含批处理 runtime / service / 路由 / `batch_task` 工具 |
| LangGraph Store（`BaseStore` 抽象） | 🔴 **已删除** | `runtime/store/{provider,async_provider}.py` |
| 普通 MCP（Tool / Server / OAuth） | ✅ **保留** | 只删了 `mcp_tasks` |
| 普通 SubAgent `task` | ✅ **保留** | 只删了 `subagent_batches` |
| memory thread metadata | ✅ **能力保留** | `MemoryThreadMetaStore` 改为简单内部 dict，**不再依赖 LangGraph `BaseStore`**（§7.8） |

> **删除依据**：`mcp_tasks` 与 `subagent_batches` 都是**默认关闭、且当前没有任何配置把它们打开的**可选 Runtime
> （`config.yaml` 中 `enabled: false`；全仓 `task_toolsets` 的声明只在 `backend/tests/`；
> `subagent_batches` 的 submitter 为 `None`、feature flag 为 `False`）。
> 删除是纯减法，不影响普通 MCP 与普通 SubAgent `task`。
> 完整证据链见过程留档 `mysql-migration-plan.md` §2.6 / §2.7 与 §16。

### 2.4 明确不在范围内的能力

以下能力**本次不迁移**。它们不是"以后再做"，而是**本次变更范围里根本不设计**：

| 能力 | 处置 | 理由 |
| --- | --- | --- |
| **LangGraph Store（`BaseStore`）** | **整体删除** | §7.8：DB 模式下"构造了但从不读写" |
| **`ag_store` 表** | 不创建 | 同上 |
| **PG 历史数据兼容设计**（`0024` 基线、重放 `0001`–`0023`、Existing Instance upgrade、backfill、数据转换、dual-read/write、数据层回滚） | 全部删除 | 本次是 fresh-cutover（§2.1） |
| **Channel / GitHub Webhook** | 已删除 | 迁移的**前置条件**（已由 G0 完成） |
| **`mcp_tasks` / `subagent_batches`** | 已删除 | §2.3 |
| **Checkpoint blob 的对象存储溢出**（inline threshold / `blob_ref` / `blob_size` / `blob_sha256` / 孤儿 GC / 写入顺序协议） | **不设计** | §7.6：`LONGBLOB` 单库即可承载 |
| **JSON → TEXT 的批量改写** | **不做** | §6.5：无兼容性/查询正确性依据 |
| **FK → 应用层级联 + 孤儿巡检** | **不做** | §6.6：InnoDB FK 原生可用 |
| **🔴 Redis 的全部用途**（checkpoint cache / delta cache / namespace / TTL / 失效钩子 / sandbox ownership 的持久化） | **整体移出本迁移方案**，转为独立跟进设计 | §2.5 |
| **`langgraph-checkpoint-mysql` 的本地 fork** | **默认不 vendor**；精确版本依赖 `==3.0.0` | §7.4 |
| **同步 MySQL CheckpointSaver** | **不实现**（上游 `pymysql.py` 类不被引用） | §7.7 |
| **🔴 生产 Runtime 中的一切 DDL** | **全部移出 Runtime**，由运维 / DBA 执行 | §11 |
| **Runtime 自动创建数据库** | **删除，且不移植到 MySQL** | §11.3.1：库不存在时 Runtime 必须启动失败 |
| **索引优化**（冗余索引清理 / query tuning / 索引合并 / 推测 workload 加索引） | 不做 | §9.4 |
| **bootstrap 分布式数据库锁** | 不做 | §11.8 |
| **通用 multi-chain migration framework** | 不建 | §11.2 |
| **双数据库测试参数化矩阵** | 不建 | §12 Goal 4 |
| **Kubernetes / Helm 的 MySQL 部署形态** | **不在本次范围** | §3.4 |

### 2.5 Redis 的边界

**Redis 不进入本迁移主线。** 具体含义：

1. 保持 `checkpoint_channel_mode = full`，不切 `delta`；
2. `database.checkpoint_cache.type` 保持默认（不生效）；
3. **不在 MySQL Saver / Schema / 键空间里提前引入 namespace 分层、失效钩子、级联驱逐、TTL 分级等结构**；
4. **不承载任何 durable truth**；
5. Redis 的性能优化 / delta 缓存 / sandbox ownership 的可靠性，**全部作为独立变更另行设计**。

> 事实基础：`database.checkpoint_cache.type` 当前是 `memory`（`config.yaml` 未配置该字段），
> 且 `CachedHistorySaver` **只在 `checkpoint_channel_mode == "delta"` 时才挂载**
> （`async_provider.py:242-253`）。两个条件同时不成立 ⇒ 缓存层**根本不在执行路径上**。

**唯一保留的结论性约束**（边界声明，不产生 MySQL 侧设计工作）：
sandbox ownership 属于 **lease / correctness state**，**不得**放入"重启即空"的 volatile Redis。

### 2.6 目标数据库边界

```
Agent Runtime
    ├── MySQL 8.0.24        ← Application Data / Checkpoint / Operational Data
    ├── Object Storage      ← Artifacts / Uploads / Tool 结果（S3/MinIO，已落地）
    ├── Knowledge Service   ← Vector / RAG（当前已是外部 RAGFlow HTTP）
    └── Remote Sandbox      ← 临时工作区
```

**这个拆分不是迁移目标，而是现状的准确描述** —— 当前项目已经满足该边界，
唯一例外是 Checkpoint 依赖 PostgreSQL。

> ⚠️ **Checkpoint payload 不进入 Object Storage**（§7.6）。上表中的
> Object Storage 只承载 Artifacts / Uploads / Tool 结果，这三项**已落地**，本次不动。

### 2.7 两层划分：Core Migration 与 Optional Compliance Pass

**本文件最重要的结构性结论。** "运行正确性"与"企业 Schema 风格"必须分开验收。

#### 第一层：Core Migration（影响运行正确性，必须做）

| # | 工作 | 判据：不做会怎样 |
| --- | --- | --- |
| 1 | **MySQL driver** —— `asyncmy`（异步主路径）+ `PyMySQL`（同步路径） | 进程起不来 |
| 2 | **ORM / 查询兼容** —— `RETURNING`、advisory lock、partial unique、JSON 方言、`DateTime` 精度 | 运行期报错或静默错误 |
| 3 | **Checkpoint 持久化** —— 固定版本复用上游 Saver + 连接池接入 | Agent Runtime 无法持久化 |
| 4 | **事务 / 锁语义** —— RC 隔离、`FOR UPDATE` 索引核验、`run_events.seq` | 并发错误 / 事件丢失 |
| 5 | **Application tables** —— 12 张表 + 框架表 | 数据无处可存 |
| 6 | **Fresh bootstrap** —— 独立链 + **运维执行的** `upgrade(head)` 迁移产物 | 库建不起来 |
| 7 | **Health / readiness** —— `mysql` 探针 + **Schema 版本校验** | 无法判断就绪；或**带着过期 Schema 启动** |
| 8 | **多实例正确性** —— 6 处能力闸门扩展 `mysql` | **进程直接 `SystemExit`** |
| 9 | **`("sqlite","postgres")` 守卫扩展** | 同上 |
| 10 | **🔴 生产迁移执行模型** —— 两份迁移产物 + Runtime 只读校验 + 调用路径隔离 | 无 DDL 权限的账号下**进程起不来** |

#### 第二层：Optional Compliance Pass（纯风格，可延后、可独立交付）

| # | 工作 | 判据：不做会怎样 |
| --- | --- | --- |
| 1 | 表名 `ag_` 前缀统一（12 张应用表 + 5 张框架表） | **无运行影响** |
| 2 | 索引命名统一（`uk_` / `idx_`） | **无运行影响** |
| 3 | 全量中文 TABLE / COLUMN COMMENT | **无运行影响** |
| 4 | 未命名唯一约束补名（`managed_subagents.name`） | **无运行影响** |
| 5 | 与公司规范的其它纯风格差异 | **无运行影响** |

#### 两层的依赖关系

```
Core Migration ──────────────────► 可独立完成并上线
       │
       │  （无依赖：Compliance Pass 不阻塞 Core）
       ▼
Optional Compliance Pass ────────► 可作为**独立变更**在任何时点交付
                                    （建议在 Core 稳定后，避免与迁移失败归因混淆）
```

**关键约束**：

1. 🔴 **Compliance Pass 不得成为 Core Migration 的前置条件。**
   唯一例外是 `version_table` 的命名 —— 它必须在 `0001_mysql_baseline` 落地时就确定（§11.2）。
2. 🔴 **不得因为 Compliance Pass 的规范要求，去否决一个运行语义可用的第三方实现**（§7.2）。
3. 🔴 **两层的验收标准分开写**：Core 用"能否正确运行"验收，Compliance 用"是否满足规范"验收。
4. 🔴 **Compliance Pass 不进入正式 Goal**（§15.5）。
   主文档**只写边界**（本节两张表），详细规范移入附录或独立文档。

---

## 3. 当前数据库架构

### 3.1 一个字段，两条职责链

`config/database_config.py:142-209`：

```python
backend: Literal["memory", "sqlite", "postgres"] = "memory"
postgres_url: str = ""
postgres_schema: str = ""
pool_size / pool_recycle / command_timeout / echo_sql
checkpoint_channel_mode: Literal["full", "delta"] = "full"
```

| backend | 应用 ORM | LangGraph Checkpointer |
| --- | --- | --- |
| `memory` | 内存实现（`get_session_factory()` → `None`） | `InMemorySaver` |
| `sqlite` | `sqlite+aiosqlite:///{sqlite_dir}/deerflow.db` | `AsyncSqliteSaver` |
| `postgres` | `postgresql+asyncpg://…` | `AsyncPostgresSaver` |

**关键事实：已有独立的 `checkpointer:` 配置节，且优先于 `database:`**
（`config/checkpointer_config.py:9` 的 `CheckpointerType`；派生逻辑见
`runtime/checkpointer/provider.py` 的 `_resolve_checkpointer_config`）：

```python
def _resolve_checkpointer_config(app_config):
    if app_config.checkpointer is not None:   # ← 独立配置优先
        return app_config.checkpointer
    # 否则按 database.backend 派生
```

⇒ **"两条职责链可分别指向不同后端"在配置层已经可表达**。
MySQL 侧只需扩展两个 `Literal`：

| 位置 | 现值 | 目标 |
| --- | --- | --- |
| `config/database_config.py:143` | `Literal["memory","sqlite","postgres"]` | 追加 `"mysql"` |
| `config/checkpointer_config.py:9` | `Literal["memory","sqlite","postgres"]` | 追加 `"mysql"` |

> 这两个 `Literal` 只造成**校验失败**，不会拒绝启动 —— 与 §12 Goal 4 的
> **6 处能力闸门**必须分开列（后者在启动期直接拒绝：3 处 `SystemExit` +
> 2 处 `ValueError` + 1 处 readiness 探针返回不可达）。

### 3.2 URL 改写与 Schema 注入（纯 PostgreSQL 语义）

| 文件 | 职责 | MySQL 侧 |
| --- | --- | --- |
| `config/database_config.py:282-294` `app_sqlalchemy_url` | `postgresql://` → `postgresql+asyncpg://` | 改为 `mysql+asyncmy://` |
| `config/database_config.py:297-319` `app_sync_sqlalchemy_url` | → `postgresql+psycopg://`（供同步 `agent_storage.backend: db`） | 改为 `mysql+pymysql://`（**必须保留**，§7.7） |
| `persistence/engine.py:117-131` | `backend == "postgres"` 时**硬性** `import asyncpg`，缺失即 `ImportError` | 改为 `import asyncmy` |
| `persistence/postgres_schema.py`（258 行） | `CREATE SCHEMA`、`search_path` 注入、libpq `options` 分词/合并/转义 | **整体不适用**（MySQL 的 schema ≡ database，无 `search_path`） |
| `config/postgres_schema.py` | `POSTGRES_SCHEMA_PATTERN` + 校验 | 不适用 |
| `persistence/engine.py:58-92` | `_auto_create_postgres_db`：连维护库 `CREATE DATABASE` | 🔴 **整段删除，且不移植到 MySQL**（§11.3.1） |
| `persistence/migrations/env.py:92-101` | 通过 `deerflow_pg_schema` 注入 search_path | MySQL 链不复用（§11.2） |

### 3.3 两条连接池，不同生命周期

- **应用侧**：SQLAlchemy `AsyncEngine` + 驱动连接池，
  `pool_size` / `pool_pre_ping` / `pool_recycle` / `command_timeout`（`persistence/engine.py:169-182`）。
- **Checkpointer 侧**：独立连接池（`runtime/checkpointer/async_provider.py:51-76`），
  当前带 `autocommit=True`、`prepare_threshold=0`、`row_factory`、TCP keepalive、`check_connection`。
- 两者**共用同一个数据库 URL，但持有各自独立的连接池**（`database_config.py:14-15` 注释明确）。
- MySQL 侧同样保持两条池（§7.4 给出 `asyncmy` 池的接入方式）。

### 3.4 部署形态与本次的部署目标

**现状**

| 项 | 现状 | 证据 |
| --- | --- | --- |
| 本机 `config.yaml` | `database.backend: postgres`、`checkpoint_channel_mode: full` | `config.yaml:211-217` |
| 模板默认 | `database.backend: sqlite` | `config.example.yaml:1794-1796` |
| 独立 `checkpointer:` 节 | **未设置**（由 `database:` 派生） | `app_config.py:323-329` 默认 `None` |
| Helm | `postgresql.enabled: true`，捆绑 `postgres:16` StatefulSet | `deploy/helm/deer-flow/values.yaml:119-131` |
| compose | `docker/*.yaml` **不含** postgres 服务 | `grep` 零命中 |
| 对象存储 | **已落地**（S3/MinIO） | `config.example.yaml:1877-1886` |
| 初始化脚本 | **没有任何 SQL schema / Docker init 脚本**；Schema 由 bootstrap 状态机生成 | `persistence/bootstrap.py` |

**本次的部署目标（按真实环境收缩）**

| 项 | 本次要做 | 说明 |
| --- | --- | --- |
| **Docker / Docker Compose** | ✅ **要做** | 在 `docker/*.yaml` 增加 MySQL 8.0.24 服务（当前连 PG 服务都没有） |
| **external MySQL DSN** | ✅ **要做** | `database.mysql_url` 支持外部 DSN；🔴 **`_auto_create_postgres_db` 直接删除，不移植**（§11.3.1） |
| **一次性迁移 job** | ✅ **要做** | 先 Application Schema，再 Checkpoint Schema（§11.0） |
| **health / readiness** | ✅ **要做** | `app/gateway/health.py` 新增 `mysql` 探针；`_probe_checkpointer_backend` 的 `Literal` 扩展 |
| **多实例运行** | ✅ **要做** | 6 处能力闸门扩展 `mysql`（§12 Goal 4） |
| **Kubernetes / Helm 的 MySQL StatefulSet** | ⛔ **不做** | 当前没有 K8s 生产需求。Helm 侧只需在 Goal 5 移除既有 PG StatefulSet 配置，**不新增 MySQL 部署形态** |

> ⚠️ **不要为了数据库迁移同时扩展不使用的部署形态。**
> 若未来确有 K8s 生产需求，MySQL 的 StatefulSet / Operator 选型应作为**独立变更**评估。

### 3.5 命名现状

- **17 张表全部无前缀**（12 张应用表 + 5 张框架表）。
- Alembic 版本表名为默认的 `alembic_version`。
- 索引命名三套并存：`ix_` × 约 27、`idx_` × 1、`uq_` × 3（+ 1 个未命名约束）。
- **0 个 TABLE COMMENT、0 个 COLUMN COMMENT**。

> ⚠️ **以上全部属于 Optional Compliance Pass**（§2.7）。
> 唯一必须计入 Core 的是 `version_table` 的命名 —— 它必须与 `0001_mysql_baseline` 同时落地（§11.2）。
> **若组织允许，最简单的做法是 MySQL 链继续使用默认表名 `alembic_version`** ——
> 它天然与 PG 链的 `alembic_version` 隔离（两条链在同一实例的不同 database 中）。

---

## 4. PostgreSQL 依赖盘点

扫描范围：`backend/app`、`backend/packages`（排除 `tests/`）、`deploy/`、`docker/`、
`config*.yaml`、`Makefile`。

### 4.1 硬依赖（不替换就跑不起来）

| # | 依赖 | 证据 | MySQL 侧 |
| --- | --- | --- | --- |
| 1 | `asyncpg`（应用 ORM 异步驱动） | `persistence/engine.py:117-131` 强制 import | 替换为 **`asyncmy`** |
| 2 | `psycopg` + `psycopg-pool` | `runtime/checkpointer/async_provider.py:53-54,96`、`app/gateway/health.py:194-215` | 替换为 **`asyncmy` 池**（§7.4） |
| 3 | `langgraph-checkpoint-postgres` | `runtime/checkpointer/{provider,async_provider}.py` | 🔻 **替换为 `langgraph-checkpoint-mysql==3.0.0`**（已冻结的精确版本依赖，§0/§7.2/§7.4）；只有 §7.3 第 4️⃣ 档被触发时才走 vendor + patch |
| 4 | 同步驱动（`psycopg`） | `persistence/agents/sql.py:52` 同步 `create_engine` | 替换为 **`PyMySQL`**（§7.7） |

### 4.2 专有 SQL（非测试生产代码，逐条）

| 文件:行 | SQL 片段 | 用途 |
| --- | --- | --- |
| `runtime/events/store/db.py:148` | `SELECT pg_advisory_xact_lock(hashtext(CAST(:thread_id AS text))::bigint)` | 事件 `seq` 分配的跨进程串行化 |
| `persistence/scheduled_task_runs/sql.py:317-318` | `SELECT pg_advisory_xact_lock(:lock_key)` | 调度器全局并发预算锁（key 常量 `_SCHEDULER_BUDGET_LOCK_KEY = 4694001`） |
| `persistence/bootstrap.py:552` | `SET LOCAL idle_in_transaction_session_timeout = 0` | 防止 DDL 期间被服务端 idle 超时杀掉 |
| `persistence/bootstrap.py:553,559` | `pg_advisory_lock(:k)` / `pg_advisory_unlock(:k)` | 跨进程 Schema bootstrap 互斥（`_PG_LOCK_KEY`） |
| `persistence/scheduled_task_runs/sql.py:203-212` | `UPDATE scheduled_tasks SET last_occurrence_seq = … RETURNING …` | 调度 occurrence 序号分配 |
| `persistence/run/sql.py:552-563` | `UPDATE runs … .returning(run_id, cancel_action)` | 租约续约 + 原子读取消意图 |
| `persistence/run/sql.py:577-594` | `UPDATE runs … .returning(cancel_action)` | 首次取消动作胜出 |
| `persistence/run/sql.py:619-627` | `UPDATE runs … .returning(run_id)` | "完成仅在取消之前"的原子判定 |
| `persistence/user/preferences.py:21-25` | `pg_insert(...).on_conflict_do_update(...)` | 逐 key 偏好 upsert |
| `persistence/migrations/versions/0018_oauth_identity_pg_partial.py:50,68` | `SELECT indpred FROM pg_index WHERE indexrelid = to_regclass(...)` | 读 PG 系统目录判断索引是否 partial（**只服务 PG 链**） |
| `persistence/json_compat.py:225-227` | `@compiles(JsonMatch)` → `raise NotImplementedError` | 自定义 JSON 匹配编译器，**只支持 sqlite / postgresql** |

### 4.3 JSON 路径表达式（`->` / `->>` 语义）

| 位置 | 表达式 |
| --- | --- |
| `persistence/run/sql.py:207` | `metadata_json["regenerate_from_run_id"].as_string()` |
| `persistence/run/sql.py:228-229` | `metadata_json["replay_kind"].as_string()`、`metadata_json["regenerate_from_run_id"]` |
| `persistence/scheduled_task_runs/sql.py:847` | `metadata_json["scheduled_task_run_id"].as_string()` |
| `persistence/thread_meta/sql.py:230,247,261` | 经 `json_match()` 生成置顶/归档过滤条件 |

> 🔴 **关键澄清**：SQLAlchemy 的 `.as_string()` 索引语法（前 3 行）
> **不是 PostgreSQL 专有** —— 它在 MySQL 方言下编译为 `JSON_UNQUOTE(JSON_EXTRACT(...))`，
> 是**方言中立**的 ORM 能力。**它们不需要任何改写**。
>
> **真正需要方言分支的只有一个文件**：`persistence/json_compat.py`。
> 它用 `@compiles` 手工生成 SQL，且默认分支
> **直接 `raise NotImplementedError`**（`json_compat.py:225-227`）⇒
> 在 MySQL 上**编译期就会大声失败**（不是静默错误，这是好事）。
>
> **全仓 `json_match` 的调用点只有 3 处，全在一个文件**：
> `persistence/thread_meta/sql.py:230`（`deerflow_pinned` 排序）、
> `:247`（`metadata_json` 过滤）、`:261`（`deerflow_archived` 排序）。
> ⇒ **整个"JSON 运算符"迁移面 = 1 个文件 / 3 个调用点 / 1 个列**。

### 4.4 Partial unique index（3 处）—— 只有 2 处需要生成列

| 索引名 | 表 | 列 | 谓词 | 业务含义 | MySQL 处置 |
| --- | --- | --- | --- | --- | --- |
| `uq_runs_thread_active` | `runs` | `thread_id` | `status IN ('pending','running')` | **每线程至多一个活跃 run**（跨进程） | 🔧 **需要生成列唯一索引** |
| `uq_scheduled_task_run_active` | `scheduled_task_runs` | `task_id` | `status IN ('queued','launching','running')` | 每任务至多一个非终态 occurrence | 🔧 **需要生成列唯一索引** |
| `idx_users_oauth_identity` | `users` | `oauth_provider, oauth_id` | `oauth_provider IS NOT NULL AND oauth_id IS NOT NULL` | 每个 OAuth 身份一个账号 | ✅ **不需要任何 workaround** |

全部同时声明 `sqlite_where` 与 `postgresql_where`。

#### 第 3 处（OAuth）为什么不需要生成列 —— 两条独立证据

**证据 A：PostgreSQL 生产环境上跑的本来就是"全量唯一索引"，不是 partial。**

`0018_oauth_identity_pg_partial.py:31-41` 的注释直接说明了这一点：

> `0001_baseline` 的 `create_index` 只传了 `sqlite_where`、**没传 `postgresql_where`**，
> 所以**所有通过 `alembic upgrade head` 供给的部署（也就是生产唯一真正跑过的路径）
> 上，`idx_users_oauth_identity` 是一个 FULL（非 partial）唯一索引** ——
> 即使 `UserRow.__table_args__` 后来补上了 `postgresql_where` 也一样
> （ORM 元数据只影响 `create_all` 新建的库，永远不影响已 versioned 的库）。

同一段注释还给出了正确性判断：

> **不是 correctness bug**（在两种后端上，唯一索引里 NULL 都不等于 NULL，
> 所以真实重复本来就被拒绝、无限多行 NULL/NULL 本来就能共存）。

`user/model.py:76-87` 进一步说明该结论**已对活的 Postgres 实例实测验证过**，
并说明 `postgresql_where` 只是为了两个更小的理由才补上：
① 让注释与实现字面一致；② partial 索引只索引非 NULL 行，随普通密码账号增长时更小更省。

**⇒ 结论：`UNIQUE (oauth_provider, oauth_id)` 在 MySQL 上不是"语义等价"，而是
"与生产 PG 今天实际运行的索引是同一个对象"。**

**证据 B：在真实 MySQL 8.0.24 上逐项实测**

| 用例 | 期望 | 实测结果 |
| --- | --- | --- |
| 4 行 `(NULL, NULL)` 普通密码账号共存 | 允许 | ✅ 4 行共存 |
| 3 行 `('github', NULL)` 共存 | 允许 | ✅ 3 行共存 |
| 2 行 `(NULL, 'oid-1')` 共存 | 允许 | ✅ 2 行共存 |
| 重复 `('github','oid-9')` | **必须拒绝** | ✅ `ERROR 1062 Duplicate entry 'github-oid-9' for key '…idx_users_oauth_identity'` |

**⇒ 语义完全等价。因此按"使用 MySQL 原生能力"：**

- ✅ **不新增 generated column**；
- ✅ **不做 `CONCAT`**；
- ✅ **不设计特殊分隔符**；
- ✅ **不改 `__table_args__` 的写法** —— 保留 `unique=True` 的 `Index(...)` 即可；
  SQLAlchemy 的 MySQL 方言会**静默丢掉** `postgresql_where` / `sqlite_where`
  （已实测：MySQL 侧渲染为 `CREATE UNIQUE INDEX idx_users_oauth_identity ON users (oauth_provider, oauth_id)`），
  而这**正是想要的结果**。

> ⚠️ **唯一需要额外做的一件事在别处**：`idx_users_oauth_identity` 这个名字被
> `app/gateway/auth/repositories/sqlite.py` 用来**区分是哪个约束被违反**，
> 而该判别逻辑在 MySQL 上会失效 —— 详见 §4.9-D。

**⇒ 生成列 workaround 的适用范围恰好 2 处**：每 thread 至多一个 active run；
每 scheduled task 至多一个 active occurrence。生成列写法已在真实 8.0.24 上验证可行（§8.4）。

### 4.5 PostgreSQL 特性使用矩阵

| 特性 | 是否使用 | 位置 |
| --- | --- | --- |
| JSON / JSONB 列 | ✅ 重度 | 应用 **11 列** + LangGraph `checkpoints.checkpoint`/`metadata` |
| JSONB 函数 / 运算符 | ✅（仅第三方 + 1 个自有文件） | checkpointer `SELECT_SQL`（`jsonb_each_text`）、DeltaChannel 动态列、`json_compat.py` |
| ARRAY | ✅（仅第三方） | `array_agg(...)`、`ANY(%s)`、`unnest(%s::text[])` |
| BYTEA | ✅（仅第三方） | `checkpoint_blobs.blob`、`checkpoint_writes.blob` |
| native UUID / ENUM / SET / Geometry | ❌ 未使用 | 用户 ID 显式 `String(36)`；状态列全部 `String(N)` + 注释枚举 |
| SERIAL / Sequence | ⚠️ 隐式 | `run_events.id` autoincrement → PG 渲染 `SERIAL`；无显式 `Sequence` |
| `RETURNING` | ✅ **4 处** | §4.2 |
| `ON CONFLICT` | ✅ **1 处** | `user/preferences.py:25` |
| CTE / Window Function | ⚠️ 仅第三方 | checkpointer DeltaChannel 两阶段 SQL |
| Partial Index | ✅ **3 处** | §4.4 |
| GIN / GiST / Expression Index | ❌ | 无 |
| Advisory Lock | ✅ **2 处事务级** + 1 处会话级 | §4.2 |
| `LISTEN` / `NOTIFY` | ❌ | — |
| `SKIP LOCKED` | ✅ **2 处** | `scheduled_tasks/sql.py:332,530` |
| `FOR UPDATE`（含 `read=True`） | ✅ **28 处** | §8.7 |
| `hashtext()` | ✅ | `runtime/events/store/db.py:148` |
| `make_interval` | ❌ 0 处 | 随渠道删除后已无 |
| `SET LOCAL idle_in_transaction_session_timeout` | ✅ | `bootstrap.py:552` |
| `CREATE INDEX CONCURRENTLY` | ✅（仅第三方） | checkpointer 3 条 |
| `CREATE EXTENSION` / pgvector | ❌ **未启用** | 从未传 `index` 配置 |
| `text_pattern_ops` | ✅（仅第三方） | PostgresStore `store_prefix_idx`（Store 整体删除 ⇒ 随之下线） |
| libpq DSN 语义 | ✅ | `postgres_schema.py` 全文 |
| PG 系统目录（`pg_index` / `to_regclass`） | ✅ | `0018_oauth_identity_pg_partial.py:50,68` |

### 4.6 违反 Query 规范的查询（必须标记）

1. `runtime/events/store/db.py:148` —— 数据库函数 `hashtext()` 承担"线程 → 锁 key"的业务映射。
2. `persistence/thread_meta/sql.py:230-261` —— 经 `json_compat.py` 生成的
   `CASE WHEN json_typeof(...) = 'number' AND (col ->> 'k') ~ '^-?[0-9]+$' THEN CAST(... AS BIGINT) END = ?`：
   deeply nested expression + PG 正则 + 类型系统依赖。
3. `persistence/run/sql.py:207,228-229`、`persistence/scheduled_task_runs/sql.py:847` ——
   JSON 路径作为 `WHERE` / `SELECT` 表达式（**方言中立，不算违规**，见 §4.3）。
4. `persistence/scheduled_task_runs/sql.py:203-212` —— `UPDATE … RETURNING` 作为序号分配原语。
5. LangGraph checkpointer 的全部 SQL（第三方，不可改，只能替换）。

> 🔴 **改写原则**：对第 2 项，**优先给 `JsonMatch` 补一个 MySQL 方言分支**
> （复用现有 `_build_clause` 机制，约 15 行），而**不是**把 11 个 JSON 列改成 `TEXT`
> 再拆 scalar 列 —— 后者是"为了一个文件的方言问题，改动 11 个列 + 5 个索引"，
> 违反"小范围适配"优先级。

### 4.7 纯 PostgreSQL 概念（MySQL 无对应物）

| 概念 | 位置 | MySQL |
| --- | --- | --- |
| `CREATE SCHEMA` + `search_path` | `postgres_schema.py`、`migrations/env.py:92-101` | schema ≡ database，无 `search_path` |
| libpq `options=-c key=value` | `postgres_schema.py:46-126` | 无对应概念 |
| `SET LOCAL idle_in_transaction_session_timeout` | `bootstrap.py:552` | 无此变量 |
| 事务级 advisory lock | §4.2 | 无等价物 |
| PG 四档行锁强度（`FOR KEY SHARE`/`FOR SHARE`/`FOR NO KEY UPDATE`/`FOR UPDATE`） | — | 🔻 **已随 `mcp_tasks` 删除而消失**，不再是迁移问题 |
| PG 系统目录 | `0018_oauth_identity_pg_partial.py:50,68` | 需改 `information_schema` 或跳过（该 revision 只服务 PG 链，MySQL 链不重放） |

### 4.8 真正的 MySQL 不兼容 vs 企业 Schema 规范

**这张表是 §2.7 两层划分的证据基础。**

| # | 事项 | 类别 | 处置 |
| --- | --- | --- | --- |
| 1 | 无 `RETURNING` | 🔴 **真不兼容** | 必须改写 4 处 |
| 2 | 无事务级 advisory lock | 🔴 **真不兼容** | 必须替换 2 处 |
| 3 | 无 partial / filtered index | 🔴 **真不兼容** | 必须生成列或函数索引替代 —— 🔴 **只有 2 处**（`runs` / `scheduled_task_runs`）；`idx_users_oauth_identity` 实测**语义等价**，直接建全量唯一索引（§4.4） |
| 4 | `JsonMatch` 无 MySQL 编译器 | 🔴 **真不兼容** | 补一个 `@compiles(JsonMatch, "mysql")` 分支 |
| 5 | `DATETIME` 默认 fsp=0（截断亚秒） | 🔴 **真不兼容** | 29 列显式 `DATETIME(6)` |
| 6 | `DateTime(timezone=True)` 静默丢时区 | 🔴 **真不兼容** | 应用层统一 UTC-naive 写入边界 |
| 7 | 默认隔离级别 RR（PG 是 RC） | 🔴 **真不兼容** | 显式 `SET SESSION … READ COMMITTED` |
| 8 | 唯一冲突错误码 `1062`（PG `23505`） | 🔴 **真不兼容** | 改错误判定路径 |
| 9 | `TEXT` 不能作主键 | ⚠️ **真约束** | checkpoint 表主键列收紧为 `VARCHAR` |
| 10 | InnoDB 索引键长 3072 字节 | ⚠️ **真约束** | 已审计，无越界（§6.9） |
| 11 | 无 `hashtext()` | ⚠️ **真不兼容** | 应用层 `sha256` 派生 64-bit key |
| 12 | **表名 `ag_` 前缀** | 🟡 **企业规范** | Compliance Pass |
| 13 | **索引命名 `uk_` / `idx_`** | 🟡 **企业规范** | Compliance Pass |
| 14 | **中文 COMMENT** | 🟡 **企业规范** | Compliance Pass |
| 15 | **禁止 `JSON` 列** | 🟡 **企业规范** | **不采纳**：MySQL 原生 `JSON` 类型可用，无正确性问题 |
| 16 | **禁止 `BLOB` / `LONGBLOB`** | 🟡 **企业规范** | **不采纳**：`LONGBLOB` 是 checkpoint payload 的正确容器（§7.6） |
| 17 | **禁止 FK** | 🟡 **企业规范** | **不采纳**：InnoDB FK 原生可用（§6.6） |
| 18 | **禁止数据库专有业务逻辑** | 🟡 **企业规范** | 部分重叠：#1–#4 恰好也是"专有 SQL"，但它们的驱动力是**不兼容**而非风格 |
| 19 | **至少一个 PK** | ✅ **已满足** | 12 张应用表 + 5 张框架表全部有 PK |

> **判据**：第 1–11 项**不做的后果是"跑不起来或静默错误"**；
> 第 12–18 项**不做的后果是"不符合公司规范"**。两类必须分开陈述、分开验收、分开排期。

### 4.9 编译通过、运行失败的静默失效面

本节全部结论来自**真实 MySQL 8.0.24 容器** + **项目自带的 SQLAlchemy 2.0.49** 双向探针
（方言编译探针 + 服务端执行探针）。

#### A. 方言编译层"静默放行"清单

| 构造 | MySQL 方言编译结果 | MySQL 8.0.24 执行结果 | 失效性质 |
| --- | --- | --- | --- |
| `UPDATE … RETURNING` | **原样输出 `… RETURNING runs.id, runs.seq`**，无警告无报错 | `ERROR 1064` 语法错误 | 🔴 **静默** |
| `INSERT … RETURNING` | **原样输出 `… RETURNING runs.seq`** | `ERROR 1064` | 🔴 **静默** |
| `DELETE … RETURNING` | **原样输出 `… RETURNING runs.id`** | `ERROR 1064` | 🔴 **静默** |
| `func.date_trunc('minute', col)` | **原样输出 `date_trunc('minute', …)`** | `ERROR 1305 FUNCTION … does not exist` | 🔴 **静默** |
| `sa.extract('epoch', col)` | **原样输出 `EXTRACT(epoch FROM …)`** | `ERROR 1064` | 🔴 **静默** |
| `INSERT … ON CONFLICT DO NOTHING` | ✅ **`UnsupportedCompilationError`（编译期就炸）** | 到不了运行期 | ✅ **响亮失败** |

> 🔴 **这是本迁移最容易踩的坑**：SQLAlchemy 的 MySQL 方言**不校验**这些构造。
> ⇒ **编译期通过、单测通过、只在运行期 1064 / 1305。**
> ⇒ Goal 3 的验收不能只看单测，**必须有真实 MySQL 上跑过一遍的集成验收**（Goal 4）。

**已确认无问题、不需要改写的**：

| 构造 | MySQL 方言渲染 | 结论 |
| --- | --- | --- |
| `FOR UPDATE` / `SKIP LOCKED` | `… FOR UPDATE SKIP LOCKED` | ✅ 渲染正确且服务端可执行 |
| `sa.JSON` 的 `.as_string()` 路径索引 | `CASE JSON_EXTRACT(col,'$."k"') WHEN 'null' THEN NULL ELSE JSON_UNQUOTE(JSON_EXTRACT(col,'$."k"')) END` | ✅ **方言中立**，`run/sql.py` / `scheduled_task_runs/sql.py` **无需改写** |
| `sa.JSON` 类型 | `JSON` | ✅ MySQL 原生类型 |
| `func.now()` | `now()` | ✅ |

#### B. 类型映射层的三个静默陷阱

| 类型 | 方言编译结果 | 真实 8.0.24 行为 | 结论 |
| --- | --- | --- | --- |
| `sa.DateTime(timezone=True)` | MySQL → **`DATETIME`**；PG → `TIMESTAMP WITH TIME ZONE` | `DATETIME` 默认 **fsp=0**；插入 `…34.123456` 后 `MICROSECOND()` 读回 **0**；`DATETIME(6)` 读回 **123456** | 🔴 **`timezone=True` 被静默忽略 + 亚秒被静默截断**（§6.5） |
| `sa.LargeBinary` | MySQL → **`BLOB`**；PG → `BYTEA` | 写入 100 KB → **`ERROR 1406 Data too long for column`**（`BLOB` 上限 64 KB）；`LONGBLOB` 正常 | 🔴 **应用 ORM 目前无 `LargeBinary` 列**（已核）；checkpoint 表由上游用 `LONGBLOB`（已实测），**两侧都安全**，但**不得在 MySQL 上引入裸 `LargeBinary`** |
| `sa.String(n)` | `VARCHAR(n)` | 一致 | ✅ |

#### C. `JSON_TYPE()` 返回**大写**

```
JSON_TYPE('1')    -> INTEGER      JSON_TYPE('"s"') -> STRING
JSON_TYPE('true') -> BOOLEAN      JSON_TYPE('null')-> NULL
JSON_TYPE('1.5')  -> DOUBLE       JSON_TYPE('{}')  -> OBJECT
JSON_TYPE('[]')   -> ARRAY
```

⇒ `json_compat.py` 的 `@compiles(JsonMatch, "mysql")` 分支里，**类型字面量必须用大写**。
用小写会让谓词**恒为假**且不报错 —— 纯静默错误（§6.5 / Gate V6）。

#### D. 🔴 授权层"约束判别"逻辑在 MySQL 上会整体失效

`app/gateway/auth/repositories/sqlite.py` 用三个判别函数把 `IntegrityError`
归因到具体约束，从而抛出契约内的 `ValueError`。**三者都只认 PG / SQLite 的错误形状**：

| 函数 | 判别依据 | MySQL 上的实际结果 |
| --- | --- | --- |
| `_driver_constraint_name` (`:32-49`) | 在 `exc.orig` / `exc.orig.__cause__` 上找 `.constraint_name` —— 这是 **asyncpg** 的属性 | asyncmy / PyMySQL **不暴露该属性** ⇒ 返回 `None` |
| `_is_oauth_identity_violation` (`:52-76`) | 回退分支要求消息里**同时**含 `oauth_provider` 与 `oauth_id` | MySQL 1062 消息是 `Duplicate entry 'github-oid-9' for key 'users.idx_users_oauth_identity'` ⇒ **不含 `oauth_provider`** ⇒ **False** |
| `_is_email_violation` (`:79-87`) | 回退分支要求消息里含 `users.email` | MySQL 消息是 `for key 'users.ix_users_email'` ⇒ **`users.email` 不是其子串** ⇒ **False** |
| `_is_uniqueness_violation` (`:90-98`) | 先看 `sqlstate` / `pgcode`（MySQL 上均为 `None`），再看 `"unique constraint failed"` / `"primary key constraint failed"` | MySQL 消息是 `Duplicate entry` ⇒ **两个子串都不匹配** ⇒ **False** |

⇒ **三个判别全部返回 `False`** ⇒ 落到函数末尾的 `raise`（`:222`），
**裸 `IntegrityError` 直接向上冒泡**，而不再是 `ValueError("OAuth account already linked: …")`
/ `ValueError("Email already registered: …")` —— **契约被破坏，用户看到的是 500 而不是冲突提示**。

**修法（Goal 3，小范围适配）**：给这三个判别补一条 MySQL 分支，
从 1062 消息里解析 `` for key `<table>.<keyname>` `` 并**与已知索引名比对**
（`OAUTH_IDENTITY_INDEX_NAME` / `_EMAIL_UNIQUE_INDEX_NAME`），
即把"按驱动属性判别"改成"按驱动属性判别，MySQL 则按 key 名判别"。
这比"往消息里找列名"更稳，因为它直接复用仓库里已有的**索引名常量**（`user/model.py:30`）。

---

## 5. MySQL 8.0.24 兼容性

### 5.1 能力核对（严格以 8.0.24 为准）

| 能力 | 8.0.24 | 说明 |
| --- | --- | --- |
| `FOR UPDATE` / `FOR SHARE` / `SKIP LOCKED` / `NOWAIT` | ✅ | 8.0.1+ |
| CTE / 窗口函数 | ✅ | 8.0.1+ / 8.0.2+ |
| 函数索引（表达式索引） | ✅ | 8.0.13+ |
| 生成列 `GENERATED ALWAYS AS (…) STORED` | ✅ | 5.7+ |
| 原生 `JSON` 类型 + `JSON_EXTRACT` / `JSON_TYPE` / `JSON_UNQUOTE` | ✅ | 5.7.8+ / 8.0 完善 |
| `json_table` / `json_keys` / `json_arrayagg` | ✅ | 8.0.4+ / 5.7.22+ |
| `LONGBLOB`（上限 4 GB） | ✅ | 受 `max_allowed_packet` 约束（§7.6） |
| `BLOB`/`TEXT` 的**表达式默认值** `DEFAULT (expr)` | ✅ | **8.0.13+** |
| `INSERT … ON DUPLICATE KEY UPDATE` | ✅ | 但**不支持** `DO UPDATE … WHERE <cond>` |
| 行别名 `INSERT … AS new` | ✅ | 8.0.19+ |
| `INSERT IGNORE` | ⚠️ 可用但**吞错误** | 会把可忽略错误降级为 warning ⇒ **仅用于确认无截断风险的列** |
| `SELECT max(…) … FOR UPDATE` | ⚠️ **语法允许，但不提供串行化** | 🔴 **已实测**：两个并发会话读到**同一个** MAX，后者插入时 `1062`（§8.6） |
| 生成列参与主键 | ✅ | 8.0 支持；但第三方包最终**不使用**该形式（§7.2） |
| **`RETURNING`** | ❌ | 不支持。🔴 **且 SQLAlchemy 2.0.49 仍会把它静默编译出来**（§4.9-A） |
| **`date_trunc()`** | ❌ | **函数不存在**（`ERROR 1305`）；SQLAlchemy 同样静默输出（§4.9-A） |
| **`EXTRACT(epoch FROM …)`** | ❌ | `ERROR 1064`；替代是 `UNIX_TIMESTAMP()`（已实测可用） |
| **Partial / Filtered Index** | ❌ | `CREATE INDEX … WHERE …` → `ERROR 1064`；需生成列 workaround（§8.4） |
| **事务级 Advisory Lock** | ❌ | 无等价物；`GET_LOCK` 是连接级（§8.2） |
| `CREATE INDEX CONCURRENTLY` | ❌ | 语法不存在；8.0 InnoDB 默认 online DDL |
| `idle_in_transaction_session_timeout` | ❌ | 只有 `innodb_lock_wait_timeout`（默认 50s） |
| `GET_LOCK` / `RELEASE_LOCK` | ⚠️ 存在但**不采用** | **已实测可用**（返回 1/1），但**连接级**、需显式释放、要求连接固定 ⇒ 不作为业务锁方案（§8.6 采用真实行锁） |
| `LAST_INSERT_ID(expr)` | ⚠️ 存在但**禁用** | 与 `AUTO_INCREMENT` 同连接互相污染（§8.3） |
| `BOOLEAN` | ⚠️ 别名 | = `TINYINT(1)` |
| `DATETIME` 时区 / 亚秒精度 | ❌ / 🔴 | 无时区感知；🔴 **默认 fsp=0，亚秒被静默截断**，必须显式 `DATETIME(6)`（§4.9-B） |
| `BLOB` 上限 | ⚠️ **64 KB** | 超限 → `ERROR 1406 Data too long`；`LONGBLOB` 4 GB（§4.9-B） |
| `max_allowed_packet` | ⚠️ 默认 **64 MB** | `LONGBLOB` 理论上限 4 GB，**实际单次写入受此值约束**（§7.6） |
| `sql_mode` 默认含 `STRICT_TRANS_TABLES` | ⚠️ 必须知道 | 截断/超长**变成硬错误**而非静默截断 ⇒ 是好事，但要求 DDL 一次到位 |
| InnoDB 索引键长上限 | ⚠️ 3072 字节 | utf8mb4 下单列 ≤ 768 字符 |
| `TEXT` 作主键 | ❌ | 主键不能用前缀长度 |
| 默认隔离级别 | ⚠️ **REPEATABLE-READ** | PG 默认 READ COMMITTED；🔴 **本项目必须显式设 RC**，且 V3 的失败是在 RC 下实测的（§8.6） |
| 唯一冲突 / 死锁错误码 | ⚠️ `1062` / `1213` | PG 是 `23505` / `40P01`；🔴 **判别逻辑见 §4.9-D** |

### 5.2 结论

**MySQL 8.0.24 足以承载 Application Data 层的全部查询模式，也足以承载 Checkpoint 层。**

需要处理的只有：

- 放弃 `RETURNING` → `rowcount` 判定 或 `SELECT … FOR UPDATE` + `UPDATE` 两步法（§8.3）；
- 放弃 partial index → 生成列唯一索引（§8.4，语义**精确等价**）；
- 放弃 advisory lock → **真实行锁**（`threads_meta` 行 `SELECT … FOR UPDATE`，§8.2 / §8.6）；
- 给 `JsonMatch` 补一个 MySQL 方言分支（§4.3，**1 个文件**）；
- 所有时间列显式 `DATETIME(6)`，应用层统一 UTC-naive 写入边界；
- **JSON 列保持 `sa.JSON` → MySQL 原生 `JSON`**（§6.5）；
- 🔴 **清除 §4.9-A 的四类"编译通过、运行失败"构造**（`RETURNING` / `date_trunc` /
  `EXTRACT(epoch)` / partial index）—— 它们**不会**在单测里暴露；
- 🔴 **补 §4.9-D 的授权层约束判别 MySQL 分支**（否则冲突错误退化为 500）。

> Checkpoint 层的可行性不再是推断：上游 22 条 migration 已逐条渲染并在真实 8.0.24 上
> **全部执行成功**，最终 schema 已导出（§7.4 / §11.4）。

---

## 6. 目标 Schema 设计

### 6.1 物理表清单

**A. 应用表（12 张，由 `Base.metadata` 拥有，139 列）**

| 表名 | 主键 | 列数 | JSON 列 | FK | partial unique |
| --- | --- | --- | --- | --- | --- |
| `agents` | `id` | 7 | 1 | — | — |
| `feedback` | `feedback_id` | 8 | — | — | — |
| `managed_subagents` | `id` | 5 | 1 | — | — |
| `personal_access_tokens` | `id` | 9 | 1 | — | — |
| `projects` | `id` | 8 | 1 | — | — |
| `run_events` | `id`（AUTO_INCREMENT） | 10 | 1 | — | — |
| `runs` | `run_id` | 31 | 3 | — | 1 |
| `scheduled_task_runs` | `id` | 16 | — | — | 1 |
| `scheduled_tasks` | `id` | 23 | 1 | — | — |
| `threads_meta` | `thread_id` | 10 | 1 | — | — |
| `user_preferences` | `(user_id, key)` | 3 | 1 | 1 | — |
| `users` | `id` | 9 | — | — | 1 |
| **合计** | | **139** | **11** | **1** | **3** |

**B. 框架表（5 张）**

| 表 | 归属 | 建表方式 |
| --- | --- | --- |
| `checkpoint_migrations` / `checkpoints` / `checkpoint_blobs` / `checkpoint_writes` | **上游 `langgraph-checkpoint-mysql` 3.0.0**（固定版本依赖，§7.4） | 由**运维执行** `database/mysql/checkpoint/` 的迁移产物创建（§11.4）；**Runtime 不创建、不 `setup()`** |
| `alembic_version`（或 `ag_alembic_version`） | **Alembic（MySQL 链）** | 由 MySQL 链的 `version_table` 创建 |

> ⚠️ `migrations/_env_filters.py:30-37` 的 `LANGGRAPH_OWNED_TABLES`
> 显式声明后 4 张为 LangGraph 所有，Alembic 必须回避。
> 🔴 **这 4 张表由上游 `MIGRATIONS`（22 条）定义，由运维执行迁移产物创建**，
> **不进入 MySQL 链的 revision、不由 Runtime 创建**（§11.4）。
> ⚠️ 默认路径下这 4 个表名是**上游的、不可改的** —— 改名的代价是
> **57 处 SQL 字符串 + `LANGGRAPH_OWNED_TABLES` + 一个单测断言**（§6.4），
> 且只有在**走 vendor 路径**（§7.3 第 4️⃣ 档）时才技术上可行。
> ⛔ **"表名不合规范"本身不构成 vendor 理由**（§7.3.3 / §7.4）。
> ⇒ **默认不改名**，把它列为一条明确的规范例外（§15.6）。

**已删除的表**：`channel_connections`、`channel_conversations`、`channel_credentials`、
`channel_oauth_states`、`webhook_deliveries`；
`mcp_tasks`、`subagent_batches`、`subagent_batch_items`（§2.3）。

### 6.2 逐表业务语义

| 表名 | 业务含义 | 归属功能 | 引入版本 |
| --- | --- | --- | --- |
| `users` | **登录账号**：邮箱、密码哈希、OAuth 绑定、`system_role`（`admin`/`user`）、`needs_setup`、`token_version`（改密后使旧令牌失效）。`id` 是 36 字符 UUID **字符串**。 | `app/gateway/routers/auth.py`、`app/gateway/auth/` | `0001_baseline` |
| `user_preferences` | **每用户的独立键值偏好**，复合主键 `(user_id, key)`。按 key 拆行是刻意的：类注释写明"独立 key 让并发客户端能补丁式修改互不相交的偏好"。 | `routers/user_preferences.py` | `0023_user_preferences` |
| `personal_access_tokens` | **API 访问令牌**（`dfp_…` 前缀）：只存 SHA-256 摘要，明文仅出现在创建响应一次；`scopes` 是路由权限子集；`expires_at`/`revoked_at`/`last_used_at` 管生命周期。 | `routers/auth.py` | `0017_personal_access_tokens` |
| `threads_meta` | **会话元数据**：展示名、状态、助手、所有者、所属项目、`metadata_json`（`deerflow_pinned`/`deerflow_archived`/`deerflow_project_id` 是前后端约定的跨端键）。 | `routers/threads.py`、`runtime/` | `0001`；`0019`/`0020` 增列 |
| `runs` | **一次 agent 运行的生命周期记录**：状态机、模型与调用参数、token 用量（含分模型明细）、列表页便捷字段、多 worker 所有权（`owner_worker_id` + `lease_expires_at`）、取消请求（`cancel_action` **先到先得**）、幂等键。 | `runtime/runs/`、`routers/runs.py` | `0001`；`0002`/`0004`/`0005`/`0008`/`0010` 扩展 |
| `run_events` | **run 的事件流**，SSE 推送与历史回放的数据源。`(thread_id, seq)` 唯一保证线程内序号连续可比；`category` 取值由 `runtime/events/catalog.py` 定义（不在库里约束）。 | `runtime/events/store/db.py` | `0001_baseline` |
| `feedback` | **用户对某次 run 的评价**：`rating` = `+1`/`-1`，可选 `comment`；`message_id` 可选。`(thread_id, run_id, user_id)` 唯一。 | `routers/feedback.py` | `0001_baseline` |
| `projects` | **用户项目**：线程通过 `threads_meta.project_id` 归属；`instructions` 是项目级上下文。刻意**没有** memory_mode / 共享 / agent 配置列 —— 没有消费方。 | `routers/projects.py` | `0019_projects` |
| `scheduled_tasks` | **定时任务定义**：调度类型与规格、时区、上下文模式（默认 `fresh_thread_per_run`）、重叠策略、下次运行时间、租约、运行计数、`last_occurrence_seq`。 | `routers/scheduled_tasks.py`、`app/scheduler/service.py` | `0003`；`0022` |
| `scheduled_task_runs` | **定时任务的每一次触发**（occurrence）。`launching` 是**短租约抢占态**，保证多 gateway 实例不重复启动同一行；`launch_accounted` 标记是否已计入父任务配额。 | 同上 | `0003`；`0007`/`0015`/`0022` |
| `agents` | **用户自建智能体定义**，`(user_id, name)` 唯一。`config` 存完整 `AgentConfig` 文档，`soul` 存人格文本。**用文档列而非逐字段列是刻意的**：`AgentConfig` 加字段不需要 schema 变更。 | `routers/agents.py` | `0006_agents` |
| `managed_subagents` | **运维统管的子代理定义**：system prompt、工具白名单、技能、模型、轮次与超时。`name` **全局**唯一，强制禁用 `task`/`ask_clarification`/`present_files`。 | `routers/subagents.py` | `0014_managed_subagents` |

**框架表**

| 表 | 业务含义 | 迁移相关性 |
| --- | --- | --- |
| `alembic_version` | 当前 schema 版本水位（单列 `version_num`） | bootstrap 启动时读它判断是否需要迁移（§11） |
| `checkpoints` | LangGraph 每个 super-step 的检查点：`checkpoint` 存图状态文档、`metadata` 存步骤来源与写入者 | 主键 `(thread_id, checkpoint_ns_hash, checkpoint_id)`（第三方包形态） |
| `checkpoint_blobs` | **通道值的序列化二进制**，按 `(thread_id, checkpoint_ns_hash, channel, version)` 存 | checkpoint 的**主体数据量**在这里（§7.6） |
| `checkpoint_writes` | 每个 task 的**待写记录**（pending writes），支撑中断/恢复与 durable execution | 与 `checkpoint_blobs` 共同构成序列化分析对象 |
| `checkpoint_migrations` | LangGraph **自己的**迁移版本表（单列 `v`），与 Alembic 无关 | 别与 `alembic_version` 混为一谈 |

### 6.3 表间关系

主干链路：

```
users ──> threads_meta ──> runs ──> run_events
                              └───> feedback
```

| 主体 | 关联到 | 基数 | 关联字段 | 库层 FK |
| --- | --- | --- | --- | --- |
| `threads_meta` | `users` | N:1 | `user_id`（可空，历史行） | 无 |
| `threads_meta` | `projects` | N:1 | `project_id` | 无 |
| `threads_meta` | `agents` / `managed_subagents` | N:1 | `assistant_id`（**存名字**，非 ID） | 无 |
| `runs` | `threads_meta` | N:1 | `thread_id` | 无 |
| `runs` | `users` | N:1 | `user_id`（可空，历史行） | 无 |
| `run_events` | `runs` / `threads_meta` | N:1 | `run_id` / `thread_id` | 无 |
| `feedback` | `runs` / `threads_meta` / `users` | N:1 | `run_id` / `thread_id` / `user_id` | 无 |
| `agents` | `users` | N:1 | `user_id` | 无 |
| `scheduled_tasks` | `users` | N:1 | `user_id` | 无 |
| `scheduled_task_runs` | `scheduled_tasks` | N:1 | `task_id` | 无 |
| `scheduled_task_runs` | `runs` | 0:1 | `run_id` | 无 |
| `personal_access_tokens` | `users` | N:1 | `user_id` | 无 |
| `user_preferences` | `users` | N:1 | `user_id`（**复合主键之一**） | **有**（`ON DELETE CASCADE`） |

> `assistant_id` 的取值是 `'lead_agent'` 或自定义智能体名，按**名字**关联到
> `agents.name` / `managed_subagents.name`，库层面没有任何约束。

**五个容易误解的点**

1. **`checkpoint_migrations` 不是 Alembic 的表** —— 项目里同时存在两套版本体系（Alembic 管应用表、LangGraph 管 checkpoint 表），必须同时交代两者的初始化路径。
2. **`managed_subagents` 与已删除的 `subagent_batches` 从来没有关系**：前者是"谁可以当子代理"的**定义**，后者是"一次批量派发"的**执行记录**。
3. **`agents` 与 `managed_subagents` 的差别是管理主体**：前者用户自建、按 `(user_id, name)` 隔离；后者运维统管、`name` 全局唯一。
4. **`threads_meta.incarnation` 是 expand-only 列，当前无任何运行时消费方**（全仓仅迁移与测试引用）。迁移设计**不要**把它当作有数据语义的列去做 backfill。
5. **`store_vectors` 根本不存在** —— 项目从未配置 embeddings，pgvector 迁移从未执行。

### 6.4 命名映射（**归入 Optional Compliance Pass**）

**Core Migration 不要求改名。** 下表仅在组织要求 Compliance Pass 时执行。

| 现名 | 目标名 | 现名 | 目标名 |
| --- | --- | --- | --- |
| `agents` | `ag_agents` | `scheduled_tasks` | `ag_scheduled_tasks` |
| `feedback` | `ag_feedback` | `scheduled_task_runs` | `ag_scheduled_task_runs` |
| `managed_subagents` | `ag_managed_subagents` | `threads_meta` | `ag_threads_meta` |
| `personal_access_tokens` | `ag_personal_access_tokens` | `users` | `ag_users` |
| `projects` | `ag_projects` | `user_preferences` | `ag_user_preferences` |
| `run_events` | `ag_run_events` | `checkpoints` | `ag_checkpoint` |
| `runs` | `ag_runs` | `checkpoint_blobs` | `ag_checkpoint_blob` |
| `alembic_version` | `ag_alembic_version` | `checkpoint_writes` | `ag_checkpoint_write` |
| | | `checkpoint_migrations` | `ag_checkpoint_migration` |

**若执行 Compliance Pass，必须同步修改的位置**：

| 位置 | 内容 |
| --- | --- |
| ORM | `__tablename__`（**12 处**） |
| Alembic 版本表 | `bootstrap.py:361` `SELECT version_num FROM alembic_version`、`:416` 的表名反射、`_get_alembic_config` 的 `version_table` |
| LangGraph 表过滤 | `migrations/_env_filters.py:28-35` `LANGGRAPH_OWNED_TABLES` |
| Migration | 新 revision 中的 `op.create_table` / `op.add_column` 的 `table=` 参数 |
| Health check | `app/gateway/health.py` |
| 🔴 **上游 Saver（checkpoint 4 张表）** | ⛔ **默认路径下改不了** —— 表名硬编码在依赖包的 SQL 字符串里。改动面是：**57 处 SQL 字符串**（`checkpoints` 32 / `checkpoint_blobs` 10 / `checkpoint_writes` 12 / `checkpoint_migrations` 3）+ `LANGGRAPH_OWNED_TABLES` + `test_persistence_migrations_env.py:60-63` 的**等值断言**。⚠️ **"表名不符合规范"不构成 vendor 理由**（§7.3.3）⇒ 要改名只能走 §7.3 第 4️⃣ 档，且**不应该为它单独触发**。**建议不改名**（列为明确的规范例外，§15.6） |

> ⚠️ **`bootstrap.py` 的 `_CANONICAL_0019_SCHEMA_FLOOR` /
> `_BASELINE_TABLE_NAMES` / `_BASELINE_INDEX_NAMES` 保持原样、不动**（§11.7）。
> 它们只服务旧 PostgreSQL bootstrap，MySQL 侧**不新增**同类常量，也**不做"缩减"**。

**主键**：**12 张应用表全部已有 PK**，合规。

- `checkpoint*` 的 `thread_id` / `checkpoint_ns` / `checkpoint_id` / `channel` / `version`
  在第三方包中**已经是 `VARCHAR(150)`**（§7.2），**不需要任何改动**。

### 6.5 类型映射

#### JSON：保持 `sa.JSON` → MySQL 原生 `JSON`

| 判断依据 | 结论 |
| --- | --- |
| MySQL 8.0.24 支持原生 `JSON` 类型吗？ | ✅ 支持（5.7.8+，8.0 完善） |
| SQLAlchemy 的 `sa.JSON` 在 MySQL 方言下渲染为什么？ | `JSON`（原生类型），**无需改动** |
| 有查询依赖 PG 专有 JSON 运算符吗？ | ❌ **只有 `json_compat.py` 一个文件**（§4.3），且它的默认分支**已经会大声报错** |
| 有 JSON 列的过滤/排序需求吗？ | ✅ 有（`threads_meta.metadata_json` 的置顶/归档），但**用 `json_match` 解决即可**，不需要拆列 |
| 改写 11 个 JSON 列的风险 | 需要引入 serialization adapter、修改 11 处 ORM、可能破坏"整文档往返"语义（`agents.config` 等） |

⇒ **决策：不改。** 只做两件小事：

1. **给 `JsonMatch` 补一个 MySQL 方言分支**（复用现有 `_build_clause`）：

```python
# persistence/json_compat.py —— 新增，约 15 行
_MYSQL = _Dialect(
    null_type="NULL",                       # ⚠️ MySQL 的 JSON_TYPE() 返回【大写】
    num_types=("DOUBLE", "INTEGER"),
    num_cast="DOUBLE",
    int_types=("INTEGER",),
    int_cast="SIGNED",
    int_guard=None,                         # MySQL 已按 INTEGER/DOUBLE 区分
    string_type="STRING",
    bool_type="BOOLEAN",
)

@compiles(JsonMatch, "mysql")
def _compile_mysql(element: JsonMatch, compiler: SQLCompiler, **kw: Any) -> str:
    if not validate_metadata_filter_key(element.key):
        raise ValueError(f"Key escaped validation: {element.key!r}")
    col = compiler.process(element.column, **kw)
    path = f'$."{element.key}"'
    typeof = f"JSON_TYPE({col}, '{path}')"
    extract = f"JSON_UNQUOTE(JSON_EXTRACT({col}, '{path}'))"
    return _build_clause(compiler, typeof, extract, element.value, _MYSQL, **kw)
```

> 🔴 **两个必须验证的细节**（Gate V6）：
> ① **MySQL 的 `JSON_TYPE()` 返回值是大写**（`'INTEGER'` / `'STRING'` / `'BOOLEAN'` /
> `'NULL'` / `'DOUBLE'` / `'OBJECT'` / `'ARRAY'`），而 SQLite 返回小写 ——
> `_Dialect` 的字面量必须大写，否则**静默不匹配**（谓词恒为 false）。
> ② MySQL 的 `JSON_TYPE` 对 `true`/`false` 返回 `'BOOLEAN'`，**不会**返回 `'INTEGER'`，
> 因此 `bool` 分支可以比 PG 简化。

2. **`personal_access_tokens.scopes`** 继续保存 `["threads:read","runs:create"]` 形态，
   **不要**变成 `threads:read,runs:create` —— 避免人为发明第二套字符串协议。

#### 时间列 → `DATETIME(6)`（**真不兼容，必须改**）

**29 个 `DateTime(timezone=True)` 列**（实测：MySQL 方言下渲染为**裸 `DATETIME`**，无 fsp）：
必须显式改为 `mysql.DATETIME(fsp=6)`，并在应用层统一
"UTC aware → naive UTC" 的写入边界转换。

#### 类型分布（139 列）

| 类型 | 列数 | 备注 |
| --- | --- | --- |
| `String(N)` | **约 68** | 全部 ≤ 1024 字符 |
| `DateTime(timezone=True)` | **29** | → `DATETIME(6)` |
| `Integer` | **约 14** | 合规 |
| `sa.JSON` | **11** | → MySQL 原生 `JSON`，**不改** |
| `Text` | **约 14** | 合规 |
| `Boolean` | **3** | → `TINYINT(1)`，语义等价 |
| `BigInteger` | 2 | `scheduled_tasks.last_occurrence_seq`、`scheduled_task_runs.occurrence_seq` |
| **合计** | **139** | |

> ⚠️ 上表的 `String` / `Integer` / `Text` / `Boolean` 为**按比例估算**；
> 精确逐列口径应在 `0001_mysql_baseline` 的 review 时重新反射一次。

### 6.6 外键：**保留**

| 位置 | 约束 | 语义 | 处置 |
| --- | --- | --- | --- |
| `persistence/user/model.py:37` | `user_preferences.user_id → users.id` `ON DELETE CASCADE` | 删用户即删偏好 | ✅ **保留**（InnoDB 原生支持 FK 与级联） |

**为什么保留 FK 是安全的**：

| 顾虑 | 事实 |
| --- | --- |
| InnoDB 支持 FK 吗？ | ✅ 完整支持，含 `ON DELETE CASCADE` / `ON UPDATE` |
| 会影响 DDL 顺序吗？ | ⚠️ 有：父表必须先建。但 `0001_mysql_baseline` 是**一次成型的单 revision**，表创建顺序由我们控制 ⇒ 无实际问题 |
| 会与"无 FK"的既有假设冲突吗？ | 不会。ORM 里只有这一处 FK，其它关联本来就是"靠 ID 字段表达" |
| 会把数据库能保证的完整性转移到业务代码吗？ | **不转移** —— 这正是保留 FK 的目的 |

**结论**：`0001_mysql_baseline` **直接创建这 1 个 FK**，ORM 的 `ForeignKey` **不动**，
**不写应用层级联删除代码，不做孤儿巡检**。

### 6.7 NOT NULL / DEFAULT

#### 审计结论：**默认不新增 `server_default`**

判定标准 —— **只有同时满足以下四条时，才值得为 MySQL 迁移新增 `server_default`**：

| # | 条件 |
| --- | --- |
| 1 | 该列**只有 ORM 写入**（不存在绕过 ORM 的 raw SQL / 外部程序插入） |
| 2 | ORM 侧**已有显式 Python `default=`** |
| 3 | DB 默认值**不承载正确性 / 并发语义** |
| 4 | 该默认值**当前 PG schema 里本来就有**（而不是为 MySQL 新加的） |

**审计结果（实测）**：在**存活表**中带 `server_default` 的字段只有 **4 个**，
且**全部是既有设计**（PG 里已有）：

| 表 | 列 | 现状 | 处置 |
| --- | --- | --- | --- |
| `scheduled_task_runs` | `attempt_count` | `default=0, server_default="0"` | ✅ **保留** |
| `runs` | `operation_kind` | `default="run", server_default=text("'run'")` | ✅ **保留** |
| `runs` | `token_usage_by_model` | `default=dict, server_default=text("('{}')")` | ✅ **保留**（MySQL JSON 默认值必须是表达式形式） |
| `scheduled_tasks` | `last_occurrence_seq` | `default=0, server_default="0"` | ✅ **保留** |

**其余全部不需要新增**：

| 类别 | 结论 |
| --- | --- |
| `runs.status` / `multitask_strategy` / 8 个计数列、`threads_meta.status` / `metadata_json`、`agents.config` / `soul`、`projects.*`、`managed_subagents.definition`、`personal_access_tokens.scopes`、`scheduled_tasks.*`、`users.*` | ⛔ **不加 `server_default`**。理由：前三条满足，但**第 4 条不成立**（PG schema 里本来就没有）⇒ 为 MySQL 单独加等于**新增一套规则** |

> 🔴 **为什么不"顺手全加"**：`server_default` 一旦加上，就形成
> **`Python default` + `DB server_default` 两套规则并存**的局面。
> 两套规则在**长期演进中必然漂移**（改了一处忘另一处），
> 而漂移的表现是"**不同写入路径得到不同的默认值**"这种极难排查的问题。
> ⇒ 原则：**只在"DB 默认值本身承载语义"时才保留 / 新增**。
>
> ✅ **审计前提已核实**：全仓绕过 ORM 的 raw `INSERT INTO` 只出现在
> `community/aio_sandbox/network_proxy.py`（自有 SQLite）与
> `agents/task_continuity/archive.py` / `deermem/core/retrieval.py`（FTS5 SQLite）——
> **没有任何一处写应用 ORM 表**。ORM 写入全部是 `session.add` / `add_all`（14 处）
> 加 1 处 `insert(UserPreferenceRow).values(...)`。⇒ 条件 1 对应用表成立。

> ⚠️ **一个语法注意点**：若将来**确实**要为
> `run_events.content`（`Text`）或 JSON 列加默认值，
> MySQL 要求写成**表达式形式** `DEFAULT ('')` / `DEFAULT ('{}')`（8.0.13+）。

**保持 nullable 的列**（`NULL` 有明确业务语义，**不得**为了"全部 NOT NULL"而造默认值）：

| 类别 | 代表列 |
| --- | --- |
| 业务上真可选 | `runs.assistant_id`、`runs.error`、`runs.stop_reason`、`threads_meta.display_name`、`users.oauth_provider`/`oauth_id` |
| **租约 / 抢占状态**（`NULL` = "未被占用"） | `runs.owner_worker_id`、`runs.lease_expires_at`、`scheduled_tasks.lease_owner`/`lease_expires_at` |
| **时间戳（尚未发生）** | `runs.cancel_requested_at`、`scheduled_task_runs.started_at`/`finished_at` |
| 取消意图（`NULL` = 未请求） | `runs.cancel_action` |

**统计**：139 列中 `nullable=False` 约 **85** 个、`nullable=True` 约 **54** 个。

> ✅ **fresh cutover 下没有 backfill 需求** —— 不存在历史行，
> `0001_mysql_baseline` 直接按最终 nullable 形态建表。
>
> ⚠️ **隐式 nullable 必须显式化**：所有 `Mapped[X | None]` 但未显式写 `nullable=` 的列，
> 以及 `Mapped[str | None] = mapped_column(String(N))` 这类"无注解也无默认"的列，
> SQLAlchemy 会从类型注解推导。迁移设计**要求逐列显式声明**，不依赖推导。

### 6.8 中文 COMMENT（**归入 Optional Compliance Pass**）

**现状：0 个 TABLE COMMENT、0 个 COLUMN COMMENT。**
MySQL 的 `COMMENT` 是表/列定义的一部分，应在 `create_table` / `add_column` 时写入。

| 表 | 表级说明 | 必须注释的关键列（示例，非全量） |
| --- | --- | --- |
| `runs` | Agent 运行记录：状态机、token 统计、多 Worker 租约、取消意图 | `status`（枚举语义）、`operation_kind`、`multitask_strategy`、`owner_worker_id`、`lease_expires_at`、`cancel_action`、`stop_reason`、`metadata_json`、`token_usage_by_model`、`follow_up_to_run_id` |
| `run_events` | 线程内事件流：单调递增 seq 分配 | `seq`、`category`、`event_type`、`content`（可能是 JSON 字符串）、`event_metadata` |
| `threads_meta` | 线程元数据（含置顶/归档） | `incarnation`、`status`、`project_id`、`metadata_json` |
| `scheduled_tasks` | 定时任务定义 | `schedule_type`/`schedule_spec`、`overlap_policy`、`context_mode`、`lease_owner`、`last_occurrence_seq` |
| `scheduled_task_runs` | 调度 occurrence 队列 | `occurrence_seq`、`launch_accounted`、`attempt_count`、`lease_owner`、`scheduled_for`、`trigger` |
| `users` / `user_preferences` | 账号与逐 key 偏好 | `system_role`、`needs_setup`、`token_version`、`oauth_provider`/`oauth_id`、`value` |
| `agents` | 自定义 Agent 定义（整文档往返） | `config`、`soul` |
| `projects` | 项目分组 | `presentation`、`instructions` |
| `personal_access_tokens` | 个人访问令牌 | `token_digest`（哈希，非明文）、`scopes`、`revoked_at` |
| `feedback` | 用户反馈 | `rating`、`message_id` |
| `managed_subagents` | 托管子 Agent 定义 | `definition` |
| `checkpoints` / `checkpoint_blobs` / `checkpoint_writes` | LangGraph checkpoint 三表 | ❌ **默认路径下加不了** —— 上游的 `MIGRATIONS` DDL 字符串是包内代码，不可编辑。⚠️ **且"没有中文 COMMENT"不构成 vendor 理由**（§7.3.3）⇒ 只能列为规范例外 |

> 🔴 **处理方式**：把它列为**明确的规范例外**（§15.6），
> **而不是为了 COMMENT 去 vendor** —— 纯风格理由已被明确排除在 vendor 判据之外（§7.3.3）。
> 若组织坚持要 COMMENT，正确做法是写进 **Optional Compliance Pass 的例外清单**，
> **不改变 CheckpointSaver 的引入方式**。

> 注释语言：全部中文（表级 + 列级）；枚举列的注释必须列出**全部合法取值及其语义**。

### 6.9 InnoDB 键长审计

按 utf8mb4（4 字节/字符）估算，上限 **3072 字节**：

| 表 | 键 | 列 | 字节 | 判定 |
| --- | --- | --- | --- | --- |
| `users` | `ix_users_email` | `email(320)` | 1280 | ✅ |
| `runs` | `uq_runs_idempotency_key` | `idempotency_key(255)` | 1020 | ✅ |
| `checkpoints` | PK | `thread_id(150) + checkpoint_ns_hash(16) + checkpoint_id(150)` | **1224** | ✅ |
| `checkpoint_blobs` | PK | `thread_id(150) + checkpoint_ns_hash(16) + channel(150) + version(150)` | 1824 | ✅ |
| `checkpoint_writes` | PK | `thread_id(150) + checkpoint_ns_hash(16) + checkpoint_id(150) + task_id(150) + idx(4)` | 1828 | ✅ |
| 其余键 | — | — | ≤ 768 | ✅ |

> ✅ **没有任何主键 / 唯一键越界。**
>
> 上游包的 schema 通过 **`checkpoint_ns_hash BINARY(16)` 进主键**
> 把"`checkpoint_ns` 会因嵌套子图增长导致索引键长超限"这个问题**从设计上消除** ——
> `checkpoint_ns` 本身放宽到 `VARCHAR(2000)`，而主键只用 16 字节的 MD5 摘要。
> ⇒ **V2 不需要实测、不需要断言兜底**（§13）。
>
> ⚠️ 仍需保留这条字节估算检查在 `0001_mysql_baseline` 的 review 中，
> 防止后续新增复合唯一键时重新越界。

---

## 7. Checkpoint 持久化设计

### 7.1 现状

| 项 | 值 | 证据 |
| --- | --- | --- |
| `langgraph` | **1.2.9** | `.venv` `importlib.metadata` 实测 |
| `langgraph-checkpoint` | **4.1.1** | 同上 |
| `langgraph-checkpoint-postgres` | **3.1.1**（已安装） | 同上 |
| `langgraph-checkpoint-sqlite` | **3.1.1**（已安装） | 同上 |
| 官方可用的 checkpoint 后端 | `base`、`memory`、`postgres`、`serde`、`sqlite` | `ls .venv/.../langgraph/checkpoint/` |
| **官方 MySQL 后端** | **不存在** | `grep -rli mysql .venv/.../langgraph/` → **零命中** |
| **第三方 MySQL 后端** | ✅ **存在且可用**（见 §7.2） | `langgraph-checkpoint-mysql` **3.0.0** |
| **引入方式** | 🔴 **已冻结**：`langgraph-checkpoint-mysql[asyncmy]==3.0.0`（§0 / §7.4） | 精确版本依赖；**不 vendor、不改源码、不 fork** |
| 当前已安装？ | ❌ 未安装（按依赖正常安装） | `ls .venv/.../site-packages \| grep langgraph_checkpoint_mysql` → 当前零命中 |

**当前 Saver**

| 路径 | Saver |
| --- | --- |
| 异步（Gateway 主路径） | `langgraph.checkpoint.postgres.aio.AsyncPostgresSaver`，连接来自独立连接池（`runtime/checkpointer/async_provider.py:129-140`） |
| 同步（`DeerFlowClient` / 测试） | `langgraph.checkpoint.postgres.PostgresSaver`，`from_conn_string`（`runtime/checkpointer/provider.py:132-147`） |
| Delta 模式包装 | `CachedHistorySaver`（`runtime/checkpointer/cached_saver.py`） |

### 7.2 已冻结 Saver 的运行语义验收

> 🔴 **本节的性质**：`langgraph-checkpoint-mysql==3.0.0` **不是"候选"**，
> 而是**本次迁移已确定的 Runtime dependency**（§0）。
> 本节是对该固定实现做**逐项运行语义核对**，为 §13 的 **V5（兼容性 / 正确性 Gate）** 提供检查清单。

#### 已冻结的选型

| 项 | 值 |
| --- | --- |
| 包名 | **`langgraph-checkpoint-mysql`** |
| 版本 | **3.0.0**（2026-01-23 发布；历史 22 个版本，从 1.0.0 起持续维护） |
| 作者 | Theodore Ni（**非 LangChain 官方组件**） |
| **许可** | **MIT**，`Copyright (c) 2024 Theodore Ni` ⇒ 默认路径下**无需随附**（依赖分发自带）；**仅当降级为 vendor 时**必须随源码保留许可全文（§7.4） |
| **引入方式** | 🔴 **已冻结：精确版本依赖**（`langgraph-checkpoint-mysql[asyncmy]==3.0.0`，写进 `pyproject.toml` 的 `mysql` extra）；⛔ **默认不 vendor**（§7.4） |
| 上游仓库 | `https://www.github.com/tjni/langgraph-checkpoint-mysql` |
| `requires_python` | `>=3.10` |
| **依赖** | `langgraph-checkpoint>=2.1.2`、`orjson>=3.10.1`、`typing-extensions>=4.12.2` |
| **依赖满足情况** | ✅ 项目装的 `langgraph-checkpoint` 是 **4.1.1 ≥ 2.1.2**，**满足** |
| **MySQL 版本要求** | **`>= 8.0.19`** ✅ 目标 8.0.24 满足 |
| 提供的 Saver 类 | `PyMySQLSaver`（同步）、`AIOMySQLSaver`（aiomysql）、**`AsyncMySaver`（asyncmy）** ✅ **正好匹配项目的异步驱动** |
| 设计声明 | 包自述：*"tries to mimic the code in langgraph-checkpoint-postgres as much as possible to enable keeping in sync with the official checkpointer implementation"* |

#### 运行语义逐项验收（**基于 3.0.0 wheel 的源码实测**）

> 证据来源：`curl` 下载 `langgraph_checkpoint_mysql-3.0.0-py3-none-any.whl` → 解包读源码。
> 关键文件：`langgraph/checkpoint/mysql/{base,aio_base,asyncmy,utils,_ainternal}.py`。

| # | 语义项 | 是否具备 | 证据 |
| --- | --- | --- | --- |
| 1 | **`aget_tuple`** | ✅ | `aio_base.py:141-204`，支持"指定 `checkpoint_id`"与"取最新"两路；`ORDER BY checkpoint_id DESC LIMIT 1` |
| 2 | **`alist`** | ✅ | `aio_base.py:75-139`，支持 `filter` / `before` / `limit`（`LIMIT %(limit)s`） |
| 3 | **`aput`** | ✅ | `aio_base.py:206-277`。**与 PG 语义一致**：primitive 通道值内联进 `checkpoints.checkpoint` JSON，其余进 `checkpoint_blobs`（`:242-249`） |
| 4 | **`aput_writes`** | ✅ | `aio_base.py:279-310`。按 `WRITES_IDX_MAP` 选择 UPSERT 或 INSERT，**与 PG 完全相同的分流逻辑**（`:295-299`） |
| 5 | **`adelete_thread`** | ✅ | `aio_base.py:312-331`，删 3 张表 |
| 6 | **pending writes** | ✅ | `_load_writes` + `deserialize_pending_writes`（`utils.py:29-38`），按 `(task_id, idx)` 排序 |
| 7 | **pending sends（`v<4` 历史迁移）** | ✅ | `aio_base.py:113-137, 189-202` + `SELECT_PENDING_SENDS_SQL`（`base.py:201-216`） |
| 8 | **thread / checkpoint namespace** | ✅ | `checkpoint_ns` 完整支持；PK 用 `checkpoint_ns_hash = UNHEX(MD5(checkpoint_ns))`（`base.py:220`） |
| 9 | **resume（中断恢复）** | ✅ | `get_tuple` 返回 `parent_checkpoint_id` 与 `pending_writes`（`aio_base.py:386-400`） |
| 10 | **retry / interrupt** | ✅ | 由 `checkpoint_writes` 的 `WRITES_IDX_MAP`（`ERROR:-1` / `SCHEDULED:-2` / `INTERRUPT:-3` / `RESUME:-4`）承载，**机制与 PG 相同** |
| 11 | **rollback** | ✅ | 依赖 `adelete_thread` + `aput_writes`；项目现有 `test_run_worker_rollback.py` 可直接回归 |
| 12 | **多轮长会话** | ✅ | 无长度/轮次限制；仅受 `checkpoint_ns VARCHAR(2000)` 约束 |
| 13 | **并发写** | ✅ | `_cursor(pipeline=True)`：`async with self.lock:`（**进程内 `asyncio.Lock`**）+ 显式 `conn.begin()` / `commit()` / `rollback()`（`aio_base.py:333-356`） |
| 14 | **`checkpoint_channel_mode=full`** | ✅ **适用** | `full` 模式**不使用 LangGraph `DeltaChannel`**（`runtime/checkpoint_mode.py:1-9` 的定义）⇒ 非 shallow 的 `AsyncMySaver` 就是正确类；包内 `ShallowAsyncMySaver` 已 deprecated |
| 15 | **`get_next_version`** | ✅ | `base.py:350-359`，`f"{next_v:032}.{next_h:016}"` |
| 16 | **`setup()`（自带 DDL 迁移链）** | ✅ | `aio_base.py:49-73` + `base.py:25-149` 的 `MIGRATIONS`（**22 条，已实测**）。⚠️ 生产 Runtime **不调用它**，改由运维执行其派生产物（§11.4） |
| 17 | **`aget_tuple` / `alist` 的 `filter`** | ✅ | `_search_where` 用 `json_contains(metadata, %(filter)s)`（`base.py:390-393`） |
| 18 | **连接池支持** | ✅ **关键** | `_ainternal.get_connection` 是**鸭子类型**：`hasattr(conn,"cursor")` → 单连接；`hasattr(conn,"acquire")` → **池**（`_ainternal.py:73-83`）⇒ **可以传入项目自己的 `asyncmy` 池** |
| 19 | **自定义 serde** | ✅ | `__init__(conn, serde=None)`（`aio_base.py:34-43`），`from_conn_string(..., serde=...)`（`asyncmy.py:39-62`） |
| 20 | **`copy_thread`** | ❌ **未实现** | 基类默认 `raise NotImplementedError`（`langgraph/checkpoint/base/__init__.py:350-372`） |
| 21 | **`prune`** | ❌ **未实现** | 同上（`:374-415`） |
| 22 | **`delete_for_runs`** | ❌ **未实现** | 同上（`:331-348`） |

#### 关于第 20–22 项：**不是缺口**（实测证明）

`langgraph-checkpoint` 4.1.1 的 `BaseCheckpointSaver` **不使用 `@abstractmethod`** ——
它用 `raise NotImplementedError`（`base/__init__.py:318,329,348,372,415`）。
⇒ **缺失这三个方法不会导致实例化失败**，只在该方法被调用时报错。

**全仓实测其调用点**：

| 方法 | 生产调用点 | 结论 |
| --- | --- | --- |
| `copy_thread` / `acopy_thread` | **0 处**。仅 `cached_saver.py:283-284, 318-319` 做**透传** | ✅ 不会被调用 |
| `prune` / `aprune` | **0 处**。仅 `cached_saver.py:287, 321-322` 透传 | ✅ 不会被调用 |
| `delete_for_runs` / `adelete_for_runs` | **0 处**。仅 `cached_saver.py:271-276, 309-311` 透传 | ✅ 不会被调用 |

**DeerFlow 真正使用的 checkpointer 方法（生产代码全量）**：

| 方法 | 生产调用点 |
| --- | --- |
| `aget_tuple` | `services.py:1116,1136,1347`、`threads.py:682`、`checkpoint_state.py:161`、`checkpoint_mode.py:146`、`worker.py:1657` |
| `alist` | `services.py:1142` |
| `aput` | `threads.py:919` |
| `aput_writes` | （由 LangGraph 运行时内部调用） |
| `adelete_thread` | `threads.py:800` |
| `get_tuple`（同步） | `checkpoint_state.py:155`、`checkpoint_mode.py:140`（`DeerFlowClient` 路径） |
| `list`（同步） | `client.py:681,735`（`DeerFlowClient`，**无生产引用**） |

⇒ **第三方 Saver 已覆盖 DeerFlow 全部真实使用的 5 个异步方法。**
`copy_thread` / `prune` / `delete_for_runs` 是 **LangGraph 平台层（`langgraph_api`）的可选能力**，
当前项目**没有任何调用路径**。

> ⚠️ **若未来引入 `langgraph_api` 的线程删除/剪枝能力**，需要重新评估这三个方法。
> 当前 `langgraph_api` 已安装（0.10.0）但**不在生产运行链上**。

#### 关于"线程 branch 复制"

⚠️ **容易误判的一处**：项目的"线程 branch / 复制"**不使用 `copy_thread`** ——
它由应用层通过 `aget_tuple` + `aput` 自行实现
（见 `tests/test_thread_regenerate_prepare.py:573,589,605`）。
⇒ **第三方 Saver 缺 `copy_thread` 不影响 branch 功能。**

#### 需要复核的三处实现细节（**不阻塞，但必须记入 V5**）

| # | 细节 | 风险 | 建议 |
| --- | --- | --- | --- |
| 1 | `UPSERT_CHECKPOINT_BLOBS_SQL` 与 `INSERT_CHECKPOINT_WRITES_SQL` 使用 **`INSERT IGNORE`**（`base.py:219, 241`） | ⚠️ `INSERT IGNORE` 会把**可忽略错误降级为 warning**（含字符串截断）⇒ 理论上存在静默丢失路径 | V5 中核对：目标列是 `LONGBLOB` / `JSON` / `VARCHAR(150)`，**截断不可能发生**；但应在 review 中确认 `VARCHAR(150)` 的取值长度确实 ≤150（`thread_id` 是 UUID/短 id，`channel` 是固定短名） |
| 2 | `SELECT_SQL` 用 `json_arrayagg(json_array(..., bl.blob))` 把二进制塞进 JSON（`base.py:169-198`），MySQL 会**自动 base64 编码并加 `base64:type251:` 前缀**，客户端 `decode_base64_blob` 还原（`utils.py:10-14`） | ⚠️ **线路膨胀约 33%**，且 DB 侧需**完整物化**整个 JSON；受 **`max_allowed_packet`** 约束 | V5 中做**大 checkpoint 的吞吐/内存实测**，并记录 `max_allowed_packet` 的实际上限（性能/容量项，不是正确性项）。**默认路径下 `SELECT_SQL` 不可改**；确需优化 ⇒ 走 §7.3 第 4️⃣ 档 |
| 3 | `BaseAsyncMySQLSaver.__init__` 调 **`asyncio.get_running_loop()`** 并存进 `self.loop`（`aio_base.py:34-43`）；同步方法是 `asyncio.run_coroutine_threadsafe(..., self.loop)` 的**转发**（`aio_base.py:403-544`），**不是 stub** | ⚠️ **必须在运行中的事件循环内构造**；若在 A 循环构造、从 B 循环调同步方法 ⇒ `InvalidStateError` | 现有 `_async_checkpointer` / `_async_checkpointer_from_database` **本身就是 async generator**（`async_provider.py:108,150`）⇒ 天然满足。**只需确认不在模块导入期构造**，并确认"图子进程"内也是先建池、再建 saver |

#### 判定

> **✅ `langgraph-checkpoint-mysql==3.0.0` 就是本次迁移的实现基线。**
>
> ⚠️ **上表的 22 项是"读源码得出的静态结论"，不能替代 V5 的真实运行验证。**
> 它把 V5 的**范围**收敛到一张明确清单（见 §7.3），但不预设 V5 一定通过。

### 7.3 V5 的职责与缺口处置阶梯

#### 7.3.1 V5 是 compatibility / correctness Gate，不是 selection Gate

```
CheckpointSaver 选型已经冻结：
    langgraph-checkpoint-mysql[asyncmy]==3.0.0

V5 的职责：
    验证这个已确定的依赖在当前 DeerFlow Runtime 中
    是否满足所需的运行语义和正确性。
```

**V5 必须覆盖的验证项**（对照 §7.2 的静态结论逐项做实）：

| 组 | 验证项 | 依据 |
| --- | --- | --- |
| **核心 5 方法** | `aget_tuple`、`alist`、`aput`、`aput_writes`、`adelete_thread` | §7.2 第 1–5 项 |
| **LangGraph 语义** | pending writes、resume、interrupt、retry、rollback、**branch / regenerate** | §7.2 第 6–11 项；branch 走 `aget_tuple` + `aput` |
| **并发与规模** | **concurrent checkpoint writes**、long conversation | §7.2 第 12–13 项（进程内 `asyncio.Lock` + `_cursor` 事务） |
| **模式匹配** | `checkpoint_channel_mode=full` ⇒ 非 shallow 的 `AsyncMySaver` 即正确类 | §7.2 第 14 项 |
| **接入正确性** | **asyncmy pool integration**（`_ainternal.get_connection` 的池鸭子类型）、**event-loop lifecycle**（`__init__` 的 `asyncio.get_running_loop()`） | §7.2 第 18 项 + 三处实现细节第 3 条 |
| **容量与边界** | **大 checkpoint payload**（吞吐 / 内存 / base64 膨胀约 33%）、**`max_allowed_packet`** 上限、`INSERT IGNORE` 风险核对 | 三处实现细节第 1–2 条；§7.6 |

> 🔴 **V5 仍然是 G2 之前的阻塞 Gate**（§12 Goal 1 / §13）。
> 但 **V5 不重新讨论"是否使用这个包"** —— 无论结果如何，实现基线都是同一个 3.0.0。

#### 7.3.2 V5 失败时的处置阶梯（**顺序固定，不得跳档**）

```
V5 发现问题
│
├─ 1️⃣ 先确认：是不是**项目接入方式**的问题？
│       · 池没传对 / 不是在运行中的事件循环内构造 / 配置分支写错 /
│         没有走 checkpoint_channel_mode=full / 未做 Schema 校验前置
│       ⇒ 修接入代码，**不动上游**。多数问题会停在这一档。
│
├─ 2️⃣ adapter / wrapper：在**不改上游源码**的前提下包一层
│
├─ 3️⃣ subclass 覆写：class DeerFlowMySQLSaver(AsyncMySaver)
│       · MIGRATIONS / UPSERT_*_SQL 是类属性（base.py:246-251）⇒ 可直接覆盖
│       · SELECT_SQL 走 _select_sql() 静态方法 ⇒ 覆写该方法
│
├─ 4️⃣ 如果**确实必须修改 package 内部 SQL / 私有实现**：
│       · vendor 3.0.0 并做**最小 patch**（§7.4 附 A/B）
│       · 改动逐条登记进 UPSTREAM.md
│       · ⚠️ 这是 fallback，**不是默认路径**
│
└─ 5️⃣ 只有出现**结构性、无法修复的 correctness 问题**时，
       才把它升级为 **architecture blocker**（档位回落，§1.4）
```

**阶梯的硬约束**：

1. 🔴 **`langgraph-checkpoint-mysql==3.0.0` 是确定的实现基线** ——
   `vendor` 只是对这个版本进行本地 patch 的 fallback，**不是与"直接依赖"并列的架构选项**；
2. 🔴 **不进行第三方 Saver 市场选型**（§0 冻结结论）；
3. 🔴 **不得跳档**：能在第 1–3 档解决的，不许直接 vendor；
4. 🔴 **不得把"来源是第三方"本身当作风险否决项** —— 它是 MIT、纯 Python，
   且**设计目标就是跟随官方 postgres 实现**；
5. 🔴 **判定"必须 vendor"或"必须自研"时，证据必须是运行语义或 correctness**，不能是风格。

#### 7.3.3 ⛔ 不构成 vendor / 自研理由的项

```
· orjson 是未使用的传递依赖
· 表名不符合规范（无 ag_ 前缀）
· COMMENT 缺失（含"没有中文 COMMENT"）
· 索引命名不符合企业风格
· JSON / BLOB 列不符合企业规范
· 使用 LONGBLOB
```

这些**要么无害，要么属于 Optional Compliance Pass**（§2.7），**都不能推翻已冻结的选型**。

### 7.4 接入方式：**默认精确版本直接依赖**

**决策：把 `langgraph-checkpoint-mysql[asyncmy]==3.0.0` 作为精确版本依赖引入。
不 vendor、不复制约 1200 行第三方源码进仓库。**

> 🔴 **vendor 的定位**：它**不是**与"直接依赖"并列的架构选项，
> 而是**对同一个 3.0.0 版本做本地 patch 的 fallback** ——
> 只在"确实必须修改 package 内部 SQL / 私有实现"时才启用（§7.3 第 4️⃣ 档）。
> ⇒ **换包 / 换实现 / 重新选型都不在选项内。**

| 维度 | **精确版本直接依赖（默认）** | vendor 源码（**同版本 patch fallback**） |
| --- | --- | --- |
| 依赖声明 | `langgraph-checkpoint-mysql[asyncmy]==3.0.0`（写进 `[mysql]` extra） | 从 extra 删掉该行，源码进仓库 |
| 传递依赖 | 额外拉入 `orjson>=3.10.1`（包内**零命中**，但它是**声明**的依赖，正常安装） | 无 |
| 本地 fork 维护 | ✅ **零** | ⚠️ 需自己维护 5 个文件 + `UPSTREAM.md` |
| 跟踪上游 diff | ✅ **不需要**（升级只改版本号） | ⚠️ 需手工 `diff` 重放 |
| import 改写 | ✅ **不需要** | ⚠️ 需改 6 处 |
| 上游修复 / 兼容升级 | ✅ `pip` 升版本即得 | ⚠️ 手工合并 |
| 源码可控性 | ❌ 黑盒：`INSERT IGNORE` / `SELECT_SQL` / 表名不可改 | ✅ 全部可改 |
| Schema 合规冲突 | ⚠️ 表名 / COMMENT 不可改 —— 但**属于 Optional Compliance Pass，不阻塞 Core**（§2.7） | ✅ 可解 |
| 供应链 | 新增 1 个非官方依赖（MIT、纯 Python、`>=8.0.19`） | ✅ 不新增 |

**🔴 进入 vendor（= 对 3.0.0 做本地 patch）的唯一判据**：

| # | 触发条件 | 判据（必须是可验证的事实，不是感觉） |
| --- | --- | --- |
| 1 | **V5 发现真实的 Runtime / correctness 缺口，且必须修改上游内部 SQL / 私有实现** | 有可复现的失败用例；且 §7.3 的第 1️⃣–3️⃣ 档**已被逐一排除** |
| 2 | **组织以书面规则禁止引入该第三方 Runtime 依赖** | 有**书面的**依赖准入规则。⚠️ 此时仍然**只能 vendor 同一个 3.0.0**（因为实现基线不换），不是换包 |

> ⛔ **明确不构成 vendor 理由的项**（同 §7.3.3）：
> `orjson` 是传递依赖 · checkpoint 表**没有 `ag_` 前缀** · 表**没有中文 COMMENT** ·
> **索引命名不符合企业风格** · 使用 **JSON** · 使用 **LONGBLOB**。

**为什么默认用依赖**：

1. **"复用成熟实现"首先意味着"走成熟的分发渠道"** —— 精确版本依赖是最低摩擦的复用方式；
2. vendor 引入的是**长期维护面**（fork 漂移、手工 diff、import 改写），
   而它换来的收益（可改表名 / COMMENT）**恰好属于不阻塞 Core 的 Compliance Pass**；
3. `orjson` 虽未被使用，但它是一个**正常安装的传递依赖**，
   **不构成"必须去掉"的缺陷**，也**不构成 vendor 的理由**；
4. 🔴 **顺序要求**：`先验证原包 → 通过则直接固定版本使用 → 确需改上游内部实现时才 vendor`。

#### 附 A：vendor 路径的落点与文件集（**仅 §7.3 第 4️⃣ 档触发时执行**）

落点在 `deerflow` 包内 ⇒ **自动进入 harness wheel**
（`harness/pyproject.toml:82-83` 的 `packages = ["deerflow"]` 已覆盖，**无需改打包配置**）：

```
backend/packages/harness/deerflow/runtime/checkpointer/mysql/
├── __init__.py        # 自写（约 12 行）：导出 AsyncMySaver / create_mysql_pool
├── pool.py            # 自写（约 30 行）：DSN 解析 + asyncmy 建池
├── LICENSE            # 上游 MIT 全文（Copyright (c) 2024 Theodore Ni）—— 必须保留
├── UPSTREAM.md        # 来源 / 版本 / commit / 本地改动清单 / 升级步骤
├── utils.py           # 上游原样
├── _ainternal.py      # 上游原样
├── base.py            # 上游，仅 1 处 import 改写
├── aio_base.py        # 上游，仅 3 处 import 改写
└── asyncmy.py         # 上游，2 处 import 改写 + 删 1 个类
```

> 📌 `__init__.py` 与 `pool.py` 是**项目自己的代码**，**两条路径都需要**。
> `LICENSE` / `UPSTREAM.md` **只在 vendor 路径**存在。

**必需集与丢弃集**：

| 上游文件 | 处置 | 理由 |
| --- | --- | --- |
| `base.py` | ✅ **必需** | `MIGRATIONS`（**22 条**）+ 7 条 SQL 常量 + `BaseMySQLSaver` |
| `aio_base.py` | ✅ **必需** | `BaseAsyncMySQLSaver`：5 个异步方法 + `_cursor` 事务 |
| `asyncmy.py` | ✅ **必需（裁剪）** | 具体 `AsyncMySaver`；删掉 deprecated 的 `ShallowAsyncMySaver` |
| `utils.py` | ✅ **必需** | `decode_base64_blob` + 3 个 `deserialize_*` + `mysql_mariadb_branch` |
| `_ainternal.py` | ✅ **必需** | `get_connection` 的**池鸭子类型识别**（关键） |
| `__init__.py` | ⛔ **丢弃** | 只提供 `BaseSyncMySQLSaver` + `Conn` 别名；同步 Saver 不启用（§7.7）⇒ 自写 |
| `_internal.py` | ⛔ **丢弃** | 只被 `__init__.py` / `pymysql.py` / `shallow.py` 引用；**异步链零引用** |
| `pymysql.py` | ⛔ **丢弃** | 同步 Saver（§7.7 不启用） |
| `aio.py` | ⛔ **丢弃** | aiomysql 驱动的 Saver（项目用 asyncmy） |
| `shallow.py` | ⛔ **丢弃** | `ShallowAsyncMySaver` 已 deprecated；`checkpoint_channel_mode=full` 不用 `DeltaChannel` |
| `langgraph/store/mysql/**` | ⛔ **丢弃** | LangGraph Store 整体删除（§7.8） |
| `py.typed` | ⛔ **丢弃** | 需要时自己加 |

⇒ **vendor 总量：5 个上游文件 + 2 个自写文件 + 2 个文档/许可文件**，
相对整包 3830 行**只取约 31%**。

#### 附 B：import 改写清单（vendor 路径唯一的源码改动，共 6 处 —— 已实测可全部命中）

| 文件:行 | 原 | 改为 |
| --- | --- | --- |
| `base.py:16` | `from langgraph.checkpoint.mysql.utils import mysql_mariadb_branch` | `from deerflow.runtime.checkpointer.mysql.utils import mysql_mariadb_branch` |
| `aio_base.py:21` | `from langgraph.checkpoint.mysql import _ainternal` | `from deerflow.runtime.checkpointer.mysql import _ainternal` |
| `aio_base.py:22` | `from langgraph.checkpoint.mysql.base import BaseMySQLSaver` | `from deerflow.runtime.checkpointer.mysql.base import BaseMySQLSaver` |
| `aio_base.py:23` | `from langgraph.checkpoint.mysql.utils import (…)` | `from deerflow.runtime.checkpointer.mysql.utils import (…)` |
| `asyncmy.py:13` | `from langgraph.checkpoint.mysql.aio_base import BaseAsyncMySQLSaver` | `from deerflow.runtime.checkpointer.mysql.aio_base import BaseAsyncMySQLSaver` |
| `asyncmy.py:14` | `from langgraph.checkpoint.mysql.shallow import BaseShallowAsyncMySQLSaver` | ⛔ **删除**（连同 `ShallowAsyncMySaver` 类与 `__all__` 中对应项） |

**保持不变的 import**（全部来自官方包，无需改写）：
`langchain_core.runnables.RunnableConfig`、`langgraph.checkpoint.base.*`、
`langgraph.checkpoint.serde.*`、`typing_extensions`（项目已装 4.15.0 ✅）。

> 🔴 **不要把 vendor 目录放回 `langgraph/checkpoint/mysql/` 的原始命名空间。**
> `langgraph` 是 **namespace package**，把第三方源码混进去会与 pip 安装的
> `langgraph-checkpoint*` 冲突且行为不可预期。**必须改名为 `deerflow.*`**。

#### 附 C：依赖影响（**默认路径**）

| 依赖 | 处置 | 说明 |
| --- | --- | --- |
| `langgraph-checkpoint-mysql==3.0.0` | ✅ **引入并固定版本** | MIT、纯 Python；`Requires-Python >=3.10`；要求 MySQL `>=8.0.19`（目标 8.0.24 满足） |
| `orjson>=3.10.1` | ⚠️ **随上游声明一并安装** | 上游声明但**包内零命中**；正常的传递依赖，**不需要为它做任何事** |
| `langgraph-checkpoint>=2.1.2` | ✅ 已装 **4.1.1** | 满足 |
| `typing-extensions>=4.12.2` | ✅ 已装 **4.15.0** | 满足 |
| `asyncmy>=0.2.10` | ✅ **新增** | 异步驱动本身（C 扩展） |
| `PyMySQL>=1.1.1` | ✅ **新增（必须保留）** | `SqlAgentStore` 的**同步**驱动（§7.7） |

**声明位置**：`backend/packages/harness/pyproject.toml` 的 `[project.optional-dependencies]`
新增 `mysql` extra（与现有 `postgres` extra 并列，`pyproject.toml:56-63`）：

```toml
mysql = [
    "asyncmy>=0.2.10",
    "PyMySQL>=1.1.1",
    "langgraph-checkpoint-mysql[asyncmy]==3.0.0",
]
```

> ⚠️ **版本用 `==` 精确固定**（不是 `>=`）。理由：Checkpoint Schema 的 DDL 由该包的
> `MIGRATIONS` 决定，而 `MIGRATIONS` 的条目数会随上游版本变化（当前 3.0.0 是 **22 条**）。
> Runtime 的 Schema 校验要拿它当基准（§11.5），**版本漂移会直接导致校验基线漂移**。

#### 许可与上游跟踪（**仅 vendor 路径必做**）

> ✅ **默认路径（固定版本依赖）下本节不适用** —— 许可随 pip 分发自带，
> 上游跟踪由版本号承担。

MIT 要求"在软件的所有副本或实质性部分中包含上述版权声明与许可声明"⇒

| 项 | 要求 |
| --- | --- |
| **`LICENSE`** | 把上游 `dist-info/licenses/LICENSE` **全文**复制到 vendor 目录（`Copyright (c) 2024 Theodore Ni`）。**不得删改** |
| **`UPSTREAM.md`** | 记录 ① 上游仓库 URL；② 版本 **3.0.0**；③ 发布日 **2026-01-23**；④ **vendor 时的上游 commit SHA**；⑤ **逐文件改动状态表**；⑥ 本地新增改动；⑦ **升级步骤** |
| **升级步骤**（写入 `UPSTREAM.md`） | ① 下载上游新版本 wheel 并解包；② `diff -u` 上游 5 个文件 vs vendor 目录；③ **重放改动状态表**（6 处 import + 裁剪 + 本地 patch）；④ 重跑 V5 回归 |
| ⛔ **不做** | 不引入自动同步工具 / 脚本 / git submodule / 补丁队列 |
| **触发复查的信号** | ① 上游修了我们 V5 发现的问题；② `langgraph-checkpoint` 大版本升级导致基类接口变化；③ 距 vendor 已满 6 个月 |

#### 改动点 1（代码）：`runtime/checkpointer/async_provider.py` 新增 `mysql` 分支

现有 PG 分支的形态（`async_provider.py:129-140`）：

```python
if config.type == "postgres":
    if not config.connection_string:
        raise ValueError(POSTGRES_CONN_REQUIRED)
    AsyncPostgresSaver, _ = _ensure_postgres_imports()
    pool = _build_postgres_pool(config.connection_string, config.postgres_schema)
    async with pool:
        await _ensure_postgres_schema_with_pool(pool, config.postgres_schema)
        saver = AsyncPostgresSaver(conn=pool)
        await saver.setup()
        yield saver
    return
```

🔴 **MySQL 分支不是"完全同构"** —— 与 PG 分支有**两处刻意的不对称**：
**不建 Schema（`_ensure_postgres_schema_with_pool` 的对应物删掉）**、
**不调 `setup()`**。

```python
if config.type == "mysql":
    if not config.connection_string:
        raise ValueError(MYSQL_CONN_REQUIRED)
    from langgraph.checkpoint.mysql.asyncmy import AsyncMySaver

    pool = await create_mysql_pool(              # ← 自写 pool.py（约 30 行）：DSN 解析 + 建池
        config.connection_string,
        autocommit=True,                         # ⚠️ 与 PG 的 autocommit=True 对齐
        maxsize=config.pool_size or 10,
    )
    try:
        saver = AsyncMySaver(conn=pool)          # ← 鸭子类型：池被 _ainternal.get_connection 识别
        # 🔴 生产路径**不调用 setup()** —— 这里不执行任何 DDL。
        #    Checkpoint Schema 由运维 / DBA 用迁移产物执行（§11.4）。
        #    这里只做「4 张表存在性 + checkpoint_migrations 版本」校验，
        #    不满足则 fail closed（§11.5），**不自动修复、不自动升级**。
        await verify_checkpoint_schema(
            pool,
            required_version=len(MIGRATIONS) - 1,   # 3.0.0 是 21；从包内常量推导，不硬编码
        )
        yield saver
    finally:
        pool.close()
        await pool.wait_closed()
    return
```

**为什么 `conn=pool` 可行**：`_ainternal.get_connection`（`_ainternal.py:73-83`）
先试 `hasattr(conn, "cursor")`，再试 `hasattr(conn, "acquire")`。
`asyncmy.Pool` 有 `acquire()`（返回异步上下文管理器）⇒ **走池分支**。
⇒ **不需要改上游源码，也不需要传单连接。**

> 🔴 **为什么必须自建池，而不能用上游的 `from_conn_string`**（实测）：
> 上游 `AsyncMySaver.from_conn_string`（`asyncmy.py:39-62`）内部是
> **`async with asyncmy.connect(...)`** —— 它给的是**单连接**，不是池。
> 单连接下所有并发 run 的 checkpoint 读写会在**同一个连接上串行化**，
> 与现有 PG 分支（`_build_postgres_pool` 建 `AsyncConnectionPool`）的能力不对等。
> ⇒ `pool.py`（DSN 解析 + `asyncmy.create_pool`）是**项目自己的代码**，
> **两条路径（依赖 / vendor）都需要它**，它**不属于 vendor 文件集**。
> 可直接复用上游 `AsyncMySaver.parse_conn_string`（`asyncmy.py:21-37`，公开静态方法）做 DSN 解析。

#### 改动点 2：`setup()` 在生产路径不调用

| 场景 | 是否调用 `setup()` | 理由 |
| --- | --- | --- |
| **生产 Runtime（Gateway）** | ⛔ **绝不调用** | 生产账号**无 DDL 权限**；调用即 `CREATE TABLE` ⇒ 权限报错或（更糟）绕过权限模型 |
| **Dev / CI / 单测 / Docker Compose** | ✅ 可以调用 | 这些环境本来就有 DDL 权限，且需要"起来即用" |
| **生产迁移产物（运维 / DBA 执行）** | ✅ 由它执行 | 内容取自同一个 `MIGRATIONS`（§11.4） |

> `setup()` 本身**确实是幂等的**（先 `CREATE TABLE IF NOT EXISTS checkpoint_migrations`，
> 再读最大 `v`、只跑增量，`aio_base.py:49-73`）——
> **但幂等性不解决权限问题**，也不满足"生产 Runtime 零 DDL"这条硬约束。
> ⇒ 判定依据不是"它安全不安全"，而是"**它是否属于 Runtime 的职责**"。**不属于。**

#### 改动点 3：`config` 的 `Literal` 与守卫

- `database_config.py:143`、`checkpointer_config.py:9` 追加 `"mysql"`；
- `health.py` 的 `_probe_checkpointer_backend` `Literal` 扩展。

#### 改动点 4：`checkpoint_channel_mode` 保持 `full`

⇒ **不挂载 `CachedHistorySaver`**（`async_provider.py:242-253` 的条件不成立）。

#### 改动点 5：同步路径

同步 Saver **不启用** ⇒ `runtime/checkpointer/provider.py` 的同步分支**不加 `mysql`**（§7.7）。

**总改动面（默认路径）**：`[mysql]` extra 1 行 + 本分支约 20 行
+ `pool.py` 约 30 行 + `verify_checkpoint_schema` 约 40 行 + 2 处 `Literal`。
**没有本地 fork、没有 import 改写、没有 `UPSTREAM.md`。**

### 7.5 Fallback：V5 缺口出现时的处置阶梯（可执行细节）

**仅在 V5 发现明确的运行语义 / correctness 缺口时启用。**
🔴 **顺序固定，不得跳档**（与 §7.3 是同一套阶梯，此处给出可执行细节）。

> ⛔ **进入本节前必须先确认：不重新开启"换不换 CheckpointSaver"的讨论。**
> 实现基线永远是 `langgraph-checkpoint-mysql==3.0.0`（§0）。

| 顺序 | 手段 | 适用条件 | 说明 |
| --- | --- | --- | --- |
| 1️⃣ | **确认是不是项目接入方式的问题** | 永远是第一步 | 逐项排查：池有没有正确传入（`_ainternal.get_connection` 要求 `hasattr(conn, "acquire")`）· saver 是否在**运行中的事件循环内**构造（`__init__` 调 `asyncio.get_running_loop()`）· `checkpoint_channel_mode` 是否 `full` · 配置分支 / `Literal` 是否漏改 · Schema 校验是否前置。**多数问题会停在这一档，且不需要碰上游** |
| 2️⃣ | **adapter / wrapper** | 缺口能被一层薄封装吸收 | **不改上游源码**：在项目侧包一层，做参数转换 / 结果后处理 / 重试与降级 |
| 3️⃣ | **subclass 覆写**（**在"精确版本依赖"下就能做**） | 缺口集中在少数可覆写的方法 / 钩子 / 类属性上 | `class DeerFlowMySQLSaver(AsyncMySaver)`，只覆写确有缺口的部分。**零 fork 成本** |
| 4️⃣ | **vendor 3.0.0 + 最小 patch** | 缺口在**模块级 SQL 常量**（`SELECT_SQL` / `UPSERT_*`）或**私有辅助函数**上，前三档确实够不到 | 改动量小（< 50 行）；逐条登记进 `UPSTREAM.md`。⚠️ 这是 **fallback**，不是默认路径 |
| 5️⃣ | **升级为 architecture blocker** | 出现**结构性、无法修复的 correctness 问题** | 档位回落（§1.4），并重新评估迁移路径 —— **这是唯一允许跳出"3.0.0 基线"的情形** |

> 🔴 **顺序要求**：**首位必须是"接入方式排查"与"零 fork 成本的子类化"**；
> 只有当缺口在模块级 SQL 常量上、前三档确实够不到时，才动用 vendor + patch。

**各手段的改动面**：

| 项 | 自研方案（已放弃） | 本方案 fallback |
| --- | --- | --- |
| 表结构 | 自研 `ag_checkpoint*` + `uk_`/`idx_` 命名 + 中文 COMMENT | **直接沿用上游 `MIGRATIONS` 的表结构**，只在确有缺口处改 |
| blob 存储 | `MEDIUMTEXT(base64)` + 阈值 + 对象存储溢出 | **`LONGBLOB` 直存不变**（§7.6） |
| SQL 重写 | 重写 `SELECT_SQL`、全部 `UPSERT_*`、DeltaChannel 两阶段 | **以上游已可运行的 MySQL SQL 为基线**，只改有缺口的部分 |
| `setup()` 迁移链 | 自研 | **沿用上游 `MIGRATIONS` 列表** |
| DeltaChannel | 需重写两阶段动态列 SQL | **不需要**（`checkpoint_channel_mode=full`） |
| **是否需要 fork 一个包** | 需要 | ❌ **不需要** —— 默认是依赖；即使降级为 vendor，也只是把源码放进仓库，不是 fork 发布 |

> ⚠️ **若走子类化（第 3️⃣ 档）**：`BaseMySQLSaver` 把 `MIGRATIONS` / `UPSERT_CHECKPOINT_BLOBS_SQL` /
> `UPSERT_CHECKPOINTS_SQL` / `UPSERT_CHECKPOINT_WRITES_SQL` / `INSERT_CHECKPOINT_WRITES_SQL`
> 都做成了**类属性**（`base.py:246-251`），可直接覆盖；
> `SELECT_SQL` 走 `_select_sql()` 静态方法 ⇒ 覆写该静态方法即可。

**若走到第 5️⃣ 档，档位仍须回到 `Feasible with significant changes`。**

### 7.6 Blob 存储：`LONGBLOB` 单库直存

#### A / B 方案比较

| | **A. MySQL 原生 `LONGBLOB` 直存**（采用） | B. inline + Object Storage overflow（已放弃） |
| --- | --- | --- |
| Schema | `blob LONGBLOB`（1 列） | `blob MEDIUMTEXT` + `blob_ref` + `blob_size` + `blob_sha256`（4 列 + 1 张映射） |
| 事务性 | ✅ **单库单事务**，原子 | ❌ MySQL + Object Storage **跨存储最终一致** |
| 失败模式 | 只有"事务成功/失败" | 需要处理"对象已写、DB 未提交"的孤儿；需要 GC |
| 读路径 | 1 次 SELECT | 1 次 SELECT + 可能 1 次对象读取 |
| 配置项 | 无 | `checkpoint.inline_blob_threshold`（需采集 workload 才能定值） |
| 容量上限 | `LONGBLOB` **4 GB**；实际受 `max_allowed_packet` 约束 | 理论无上限 |
| 写放大 | 无 | base64 内联部分 +33% |
| 运维复杂度 | 低 | 需要 GC 任务 + 孤儿监控 + 阈值调优 |
| **正确性风险** | **低** | 中（跨存储不一致） |

#### 判定

> **✅ 采用 A（`LONGBLOB` 单库直存）。**
>
> **不实现**：Object Storage overflow、`inline_blob_threshold`、`blob_ref`、
> `blob_size`、`blob_sha256`、孤儿 GC、5 步写入顺序协议。

**依据**：

1. **上游包的 schema 已经就是这么做的** —— `base.py:45,56` 写的是
   `` `blob` LONGBLOB ``，且它跟随官方 postgres 实现（官方 PG 用 `BYTEA` 直存，**没有 overflow 机制**）。
   🔴 **已实测**：把上游 22 条 migration 在真实 MySQL 8.0.24 上执行后，
   `mysqldump` 导出的 `checkpoint_blobs.blob` / `checkpoint_writes.blob` 均为 **`longblob`** ——
   **不需要改任何 DDL**。
2. **MySQL `LONGBLOB` 上限 4 GB**，远超任何现实的 checkpoint payload。
3. **`MEDIUMTEXT` 路线的原始动机是 `TEXT` 的 64 KB 上限** ——
   这个动机只在"禁止 BLOB"的前提下存在。🔴 反过来，**裸 `BLOB` 才是真陷阱**：
   已实测 100 KB 写入 `BLOB` 列 → `ERROR 1406 Data too long`（§4.9-B）。
   ⇒ **只要列类型是 `LONGBLOB` 就安全；只要有人把它改成 `BLOB` 就会立刻炸。**
4. **跨存储一致性是本设计里最贵的一项复杂度**，而它解决的是**尚未发生的容量问题**。

**唯一需要落实的运维项**：

| 项 | 说明 |
| --- | --- |
| **`max_allowed_packet`** | ⚠️ 单个 checkpoint blob 必须能装进一个 packet。🔴 **已实测 8.0.24 服务端默认值 = `67108864`（64 MB）**。⇒ **部署 checklist 必须显式确认该值**，并在应用侧对单 blob 加**软上限告警**（不是硬截断） |
| **大 payload 监控** | 采集 `checkpoint_blobs` 的 `LENGTH(blob)` 分布（`max` / `p99`），作为**是否需要 overflow 的实证依据** |
| **列类型守卫** | ⚠️ 任何未来改动**不得**把 `blob` 列从 `LONGBLOB` 降级为 `BLOB`/`MEDIUMBLOB` —— 降级后会在**写入大 checkpoint 时**才失败（`1406`），且不会在单测里暴露 |

> ⚠️ **Object Storage 只作为后续独立优化**：当且仅当实测出现
> "单 blob 逼近 `max_allowed_packet`"或"DB 存储成本不可接受"时，
> 才把 overflow 作为**独立变更**引入。
>
> **这不影响已存在的 Artifact / Upload Object Storage**（§2.6 / §10.3），
> 这里只讨论 checkpoint payload。

### 7.7 只启用 async CheckpointSaver

**决策：第一阶段只启用 async CheckpointSaver，不启用同步 MySQL Saver。
但同步数据库驱动必须保留。**

> 🔻 **复用上游包带来的简化**：同步 Saver（`PyMySQLSaver` / `AIOMySQLSaver`）**根本不会被引用** ——
> 只从 `langgraph.checkpoint.mysql.asyncmy` 导入 `AsyncMySaver`，
> ⇒ "不实现"变成"**不导入**"，工作量归零。
> （上游包里确实存在同步类，但**不引用即不进入执行路径**。）
>
> ⚠️ 若因 §7.4 判据降级为 vendor，则**只复制异步链的 5 个文件**，
> `pymysql.py` / `aio.py` / `__init__.py` / `_internal.py` / `shallow.py` **都不进仓库**（§7.4 附 A）。

#### 同步 Saver 的消费者（完整清单）

| 消费者 | 位置 | 分类 | 是否生产 |
| --- | --- | --- | --- |
| **`DeerFlowClient`** | `client.py:145`（定义）、`:473`/`:621`/`:920`（构造同步 checkpointer） | **同步 API**（`def stream` / `def chat`，无 async 版本） | ❌ **无任何生产引用** —— 全仓 grep 在 `app/` 与 `packages/harness/deerflow/` 内**零命中** |
| TUI | — | — | ❌ **代码已删除**（`tui/` 仅剩空目录；`pyproject.toml` 无 `[project.scripts]`） |
| CLI | `extensions/cli.py`、`skills/review/cli.py`、`scripts/benchmark/…/cli.py` | — | ❌ 均**不构造 checkpointer** |
| 示例脚本 | `scripts/e2e_safety_termination_demo.py:107` | 脚本 | ❌ 非生产 |
| 配置热重载 | `config/app_config.py:503-506` `reset_checkpointer()` | 单例重置 | ⚠️ 只重置，**不建 saver** |
| health | `app/gateway/health.py:115` | 仅配置解析 | ✅ 生产，但**不建 saver** |
| 测试 | `test_checkpointer.py`、`test_client*.py` 等 | 测试 | — |

⇒ **不需要为它启用同步 MySQL Saver。**

#### 🔴 但"同步 Saver"与"同步驱动"是两件事

| | 同步 **Saver** | 同步**数据库驱动** |
| --- | --- | --- |
| 谁需要 | 只有 `DeerFlowClient`（调试） | **`agent_storage.backend: db`** —— `SqlAgentStore` |
| 证据 | `client.py:473,621,920` | `persistence/agents/__init__.py:56` 构造 `SqlAgentStore(config.database.app_sync_sqlalchemy_url)`；`persistence/agents/sql.py:50-52` **同步** `create_engine(...)`（模块级 `_engines` 缓存） |
| 调用方 | — | `routers/agents.py:265,352,468,541`、`routers/subagents.py`、`setup_agent_tool.py:61`、`update_agent_tool.py:227`、`config/agents_config.py:213,241,261`、`subagents/registry.py:66` |
| 关键约束 | — | `get_agent_store` 的 docstring 明写 **"the per-run agent build runs in the graph subprocess"** ⇒ **图子进程用同步方式构建 agent** |
| **处置** | ❌ **不启用** | ✅ **必须保留**（MySQL 侧用 `PyMySQL`） |

> 🔴 **这是最容易做错的一处**：看到"同步路径没有生产消费者"就顺手把同步驱动也去掉，
> 会让 `agent_storage.backend: db` 整条链（agent / managed-subagent 定义的读写）**直接坏掉** ——
> 它与 checkpointer 无关，是**独立的同步驱动刚需**。

#### 驱动结论

```
asyncmy  → 异步主路径（Gateway / Agent Runtime / async Checkpointer / RunEventStore）
PyMySQL  → 同步路径，唯一用途：SqlAgentStore（graph subprocess 里的 agent 定义读取）
```

**两条禁令**：

- ⛔ **不要因为引入了 `PyMySQL` 就顺手启用"同步 MySQL CheckpointSaver"。**
- ⛔ **不要为"未来可能有别的同步消费者"提前把同步 Saver 也接上。**

**验收**：`grep -rn "app_sync_sqlalchemy_url"` 的**每个**调用点都要有明确结论；
若最终只剩 `persistence/agents/` 一处，则 `PyMySQL` 的引入范围就是这一处。

### 7.8 LangGraph Store：整体删除

**决策：不迁移 LangGraph Store，直接删除。不创建 `ag_store`。**

#### 五个"看起来像 Store"的概念必须分开

| # | 名称 | 实现 / 位置 | 存储 | 依赖 PG | 生产运行链 | 处置 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | **LangGraph Store**（`BaseStore`） | `runtime/store/{provider,async_provider,__init__,_sqlite_utils}.py` | 跟 `checkpointer:` / `database:` 走 | ✅ | ❌ **只构造、不读写** | 🔴 **删除** |
| 2 | **RunStore** | `runtime/runs/store/{base,memory}.py` + `persistence/run/sql.py` | `runs` 表 | ✅ | ✅ 是 | ✅ **保留并迁移** |
| 3 | **ThreadMetaStore** | `persistence/thread_meta/*` | `threads_meta` 表（DB 模式）/ `MemoryThreadMetaStore`（memory 模式，**内部用 `BaseStore`**） | ✅ | ✅ 是 | ✅ **保留并迁移** |
| 4 | **Agent Memory** | `agents/memory/`（`deermem` / `mem0` / `honcho` / `openviking` / `noop`） | **本地文件 + SQLite FTS5** | ❌ **完全无关** | ✅ 是 | ✅ **保留，本来就不动** |
| 5 | **Agent definition persistence** | `persistence/agents/*` + `persistence/managed_subagents/` | `agents` / `managed_subagents` 表或文件 | ✅（走**同步** SQLAlchemy） | ✅ 是 | ✅ **保留并迁移** |

> ⚠️ **上游包也提供了一个 `langgraph/store/mysql/`**（`Store` 的 MySQL 实现）。
> **本方案完全不使用它** —— 因为 LangGraph Store 整体删除。
> 默认路径（依赖引入）下它随包安装但**不被 import**；降级为 vendor 时**一并丢弃**（§7.4）。
> ⇒ 代码里**不会出现**这个模块的任何引用，也不会建任何 Store 表。

#### 为什么可以整体删除（实测证据）

| 断言 | 实测结论 |
| --- | --- |
| `setup_agent_tool` 从 LangGraph Store 读 agent 定义 | ❌ **否**。`tools/builtins/setup_agent_tool.py:9` `from deerflow.persistence.agents import get_agent_store`、`:61` `store = get_agent_store()`、`:73` `store.update(...)` —— 用的是**自研 `AgentStore`**（file/db），与 `BaseStore` 无关。同源调用还有 `update_agent_tool.py:227`、`routers/agents.py:468`、`config/agents_config.py:213,241,261` |
| Store 里存在 `agents` 命名空间 | ❌ **未找到任何写入点**。全仓对 `BaseStore` 的 `aput/aget/asearch/adelete` 生产调用**只有两处**，namespace 都是 `("threads",)` |
| Store 实际承载什么 | ✅ **memory 模式下的 `("threads",)` 线程元数据**：`persistence/thread_meta/memory.py:21` `THREADS_NS = ("threads",)` |
| DB 模式下 Store 被谁读写 | ⚠️ **只被"构造 + 挂载"，不被读写**：`deps.py:435` **无条件** `make_store(config)`；`worker.py:1257`、`checkpoint_state.py:129`、`services.py:1195` 均**只挂载**；`deps.py:498` 的 `make_thread_store(sf, app.state.store)` 因 `sf is not None` 而**选中 SQL 实现**，`store` 参数被忽略（`thread_meta/__init__.py:36-47`） |
| 唯一真正读 Store 的生产路径 | `app/gateway/app.py:129-171` 的**孤儿线程迁移**（`asearch(("threads",))` + `aput`）—— 它是"无鉴权 → 有鉴权"的历史升级路径，且**非致命**（异常仅记日志）。**fresh cutover 没有孤儿数据，该路径无意义** |
| 是否有 vector / semantic / namespace 树 | ❌ 无。`runtime/store/` 内 grep `index=` / `embedding` / `vector` / `semantic` → **0 命中**；`search()` 的两处调用**都不带 `query`** |

> **结论**：在 `database.backend: postgres`（⇒ MySQL）下，LangGraph Store 是一份
> **"构造了但没有任何生产读写"** 的设施；它唯一的真实用途是 **memory 模式的线程元数据兜底**。

#### 删除清单（**3 处非显然耦合必须一起处理**）

| # | 改动 | 位置 | 备注 |
| --- | --- | --- | --- |
| 1 | 删除 `runtime/store/{__init__,provider,async_provider}.py` | `runtime/store/` | |
| 2 | 🔴 **`_sqlite_utils.py` 必须保留 / 迁走，不能随目录删** | `runtime/store/_sqlite_utils.py` | `resolve_sqlite_conn_str` / `ensure_sqlite_parent_dir` 被 **`checkpointer/provider.py:33`、`checkpointer/async_provider.py:34`、`app/gateway/health.py:176`** 复用 ⇒ 移到 `runtime/checkpointer/` 或 `runtime/sqlite_utils.py` |
| 3 | 删除 `deps.py:379` 的 import 与 `:435` 的 `app.state.store = ...make_store(config)` | `app/gateway/deps.py` | 它是**无条件构造** —— 删掉即彻底消除"要不要写 MySQL Store"这个问题 |
| 4 | 删除 `store=` 管线（**5 处**） | `deps.py:716`、`worker.py:806/1257`、`checkpoint_state.py:129`、`services.py:1195` | 纯挂载，无读取 |
| 5 | 🔴 **`make_thread_store(sf, app.state.store)` → `make_thread_store(sf)`** | `deps.py:498`、`persistence/thread_meta/__init__.py:36-47` | 该工厂的 `store` 参数**只为 memory 模式服务** |
| 6 | 🔴 **`MemoryThreadMetaStore` 改为内部 dict 实现** | `persistence/thread_meta/memory.py` | 见下方"决策 6" |
| 7 | 删除孤儿线程迁移 | `app/gateway/app.py:129-171`（`_iter_store_items` / `_migrate_orphaned_threads`） | 历史升级路径，fresh cutover 下无意义 |
| 8 | 删除 `reset_store()` 调用 | `config/app_config.py:504,507` | 配置热重载时重置 Store 单例 |
| 9 | 删除 re-export | `runtime/__init__.py:12` | |
| 10 | `health.py` 去掉 Store 探针 | `app/gateway/health.py` | 保留 `_sqlite_utils` 的用法 |
| 11 | 测试调整 | `test_pg_schema_integration.py:16-17`、`test_app_config_reload.py:25`、`test_checkpointer.py`（多处）、`blocking_io/test_gate_smoke.py:28,49` | |

#### 决策 6：`MemoryThreadMetaStore` 的处置

**背景事实（实测）**：

| 断言 | 实测结论 |
| --- | --- |
| 生产调用点 | **只有 1 处**：`deps.py:496-498` 的 `make_thread_store(sf, app.state.store)`。DB 模式下 `sf is not None` ⇒ **选中 `ThreadMetaRepository(sf)`**，`MemoryThreadMetaStore` **根本不被构造** |
| 实际消费者 | **全部是测试**：`test_threads_router.py`（最主要，约 20 处直接访问 `THREADS_NS`）、`test_gateway_services.py`（8 处）、`test_memory_thread_meta_isolation.py`、`test_conversation_access.py`、`test_auth.py`、`test_thread_run_keep`、`test_gateway_checkpoint_mode.py`、`test_thread_archive.py`、`test_stateless_runs_owner_isolation.py`、`test_thread_run_idempotency.py`；以及 `scripts/benchmark/checkpoint/bench_production.py:495,517` |
| 它用到 `BaseStore` 的哪些方法 | **恰好 4 个**：`aget` / `aput` / `adelete` / `asearch` |
| memory backend 还有价值吗 | ✅ **有**。`database.backend: memory` 是受支持的开发模式，且 `test_threads_router.py` 是 `/api/threads` 的**主要测试夹具** —— 删掉它等于删掉一个**仍在被大量使用**的能力 |

**决策：保留能力，把 `BaseStore` 换成内部 dict 实现。**

> **不选"删除"的原因**：memory backend 是**活的 dev/单测能力**。
> 按决策优先级，**"删除不需要的能力"的前提是"不需要"**；这里它**被需要**。
> 而保留它的成本已被压到极低：只需一个约 35 行的 dict 后端
> （同样的 4 个方法 + 一个带 `.key` / `.value` 的轻量记录对象）。
>
> 🔴 **不保留整个 LangGraph `BaseStore` 抽象的原因**：全仓 `langgraph.store.base.BaseStore`
> 的**唯一**消费者就是这一个类；为了一个小能力留住整个 Store 抽象，
> 与"删除不需要的 abstraction"直接冲突。

**改动面（很小）**：

| # | 改动 |
| --- | --- |
| 1 | `persistence/thread_meta/memory.py`：把 `from langgraph.store.base import BaseStore` 换成自带的 `_InMemoryKV`（约 35 行，暴露 `aget/aput/adelete/asearch`，记录对象带 `.key` / `.value`） |
| 2 | `persistence/thread_meta/__init__.py`：`make_thread_store(session_factory)` —— **去掉 `store` 参数**；`sf is None` 时构造 `MemoryThreadMetaStore()`（内部自建 dict） |
| 3 | `deps.py:498`：`make_thread_store(sf, app.state.store)` → **`make_thread_store(sf)`** |
| 4 | 测试：`MemoryThreadMetaStore(InMemoryStore())` → `MemoryThreadMetaStore()`（**机械替换**；直接访问 `store.aget(THREADS_NS, …)` 的断言**保持不变**，因为新后端暴露同名方法） |
| 5 | `scripts/benchmark/checkpoint/bench_production.py:495,517` 同步调整 |

**删除后的三条保证**：

1. **Gateway 启动不依赖 Store** —— `deps.py:435` 移除后，启动期只剩 checkpointer + engine + repositories。
2. **Agent 主运行链不依赖 Store** —— 三处 `graph.store = store` / `agent.store = store` 是挂载。
   ⚠️ **实施时必须回归验证**：确认**没有任何 graph node / tool 通过 LangGraph 运行时注入读 `store`**
   （已 grep：`agents/` 与 `app/` 内**无** `BaseStore` 读写点）。
3. **Agent 定义读取不受影响** —— 走 `get_agent_store()`（`agents` 表 / 文件），与 Store 无关。

#### pgvector / Vector Search

- **pgvector 未启用**：`runtime/store/async_provider.py` 只传 `conn_string`，**从不传 `index`**，
  因此 `PostgresStore` 的 `VECTOR_MIGRATIONS` 从未执行，`CREATE EXTENSION vector` 从未运行。
- **Store 删除后本项彻底消失** —— 不需要任何 MySQL Vector 讨论。
- **RAG 不在数据库里**：通过 `deerflow/community/ragflow/` 以 **HTTP 调用外部 RAGFlow 服务**。
- 本地检索走 **SQLite FTS5**（`deermem/core/retrieval.py`），与关系库无关。

---

## 8. 并发语义替换

### 8.1 隔离级别与错误模型

| 项 | PostgreSQL | MySQL 8.0.24 / InnoDB | 影响 |
| --- | --- | --- | --- |
| 默认隔离级别 | READ COMMITTED | **REPEATABLE READ** | 快照语义变化；gap lock 引入 |
| 显式设置 | 无（代码未设置 `isolation_level`） | 无 | 需显式 `SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED`，否则所有并发假设需重新证明 |
| 唯一约束冲突 | SQLSTATE `23505` | 错误码 **1062** | 🔴 错误判定路径必须改写：`app/gateway/auth/repositories/sqlite.py` 的**三个判别函数在 MySQL 上全部返回 `False`**（已实测，§4.9-D） |
| 死锁 | 立即检测 + `40P01` | `innodb_deadlock_detect` 默认 ON，抛 **1213** | `innodb_lock_wait_timeout` 默认 **50 秒**（PG 的 `lock_timeout` 默认无限） |
| 语句超时 | `command_timeout=30` 通过 asyncpg `connect_args` | 需 `max_execution_time`（仅 SELECT）/ `innodb_lock_wait_timeout` | `_postgres_engine_kwargs`（`persistence/engine.py:30-50`）整个函数需重写 |

### 8.2 Advisory Lock（2 处事务级）

| 位置 | 现语义 | MySQL 替代 |
| --- | --- | --- |
| `runtime/events/store/db.py:148`（事件 `seq` 分配） | `pg_advisory_xact_lock(hashtext(thread_id))` | ✅ **已定**：先对 **`threads_meta` 行**取 `SELECT … FOR UPDATE` 行锁（锚点不存在时先幂等建行），再读 `max(seq)`（§8.6，**已实测有效**） |
| `persistence/scheduled_task_runs/sql.py:317`（调度器全局预算） | `pg_advisory_xact_lock(4694001)` | **锁表 sentinel row + `SELECT … FOR UPDATE`**（与上一条同一模式），或 unique constraint + retry |

**为什么不用 `GET_LOCK()`**：它是 **connection-scoped** 而非 transaction-scoped，
连接归还池时锁不释放 → 可能永久死锁。
（`GET_LOCK` / `RELEASE_LOCK` **本身在 8.0.24 上可用** —— 已实测返回 `1`/`1`；
不采用的理由是**作用域不匹配**，不是"不可用"。）

**`hashtext()` 的替代**：MySQL 无 `hashtext`，`CRC32()` 只有 32 位 ⇒
必须在**应用层**用 `sha256` 派生 64-bit key。

**bootstrap 的会话级 advisory lock**：**不迁移、不实现替代**（§11.8）。

### 8.3 `RETURNING`（4 处）

| 位置 | 语义 | MySQL 替代 | 难度 |
| --- | --- | --- | --- |
| `persistence/run/sql.py:563` `renew_lease` | 续约成功与否 + 读回 `cancel_action` | `UPDATE … WHERE …` → `result.rowcount == 1` 判定续约；随后**同事务内** `SELECT cancel_action WHERE run_id=?`（该行已被 X 锁，读值一致） | Easy |
| `persistence/run/sql.py:594` `request_cancel` | 读回"首次胜出"的 `cancel_action` | 同事务内 `SELECT … FOR UPDATE` 取当前值 → Python 计算 `case` 结果 → `UPDATE` 写入 | Easy |
| `persistence/run/sql.py:627` `finalize_if_not_cancelled` | 是否完成了 finalize | `result.rowcount == 1`（纯条件 UPDATE，无 upsert）—— **已是最简形式** | Easy |
| `persistence/scheduled_task_runs/sql.py:211` `occurrence_seq` | 序号分配 | **两步法**：`SELECT last_occurrence_seq … FOR UPDATE` → Python `+1` → `UPDATE` | Moderate |

> 🔴 **不要用 `LAST_INSERT_ID(expr)`**：`run_events.id` 是 `AUTO_INCREMENT`，
> 同一连接/事务内的 INSERT 会覆盖 `LAST_INSERT_ID()` 的值，导致**序号串号**。

**UPSERT 对照**

| 用法 | PG | MySQL |
| --- | --- | --- |
| `ON CONFLICT DO NOTHING` | ✅ | `ON DUPLICATE KEY UPDATE col = col`（**推荐**；`INSERT IGNORE` 吞所有错误，含截断） |
| `ON CONFLICT DO UPDATE` | ✅ | `ON DUPLICATE KEY UPDATE`（用行别名 `AS new`，`VALUES()` 已废弃） |

**🔴 `UPDATE … RETURNING` 经 SQLAlchemy 编译为含 `RETURNING` 的 SQL，且不报错**（实测）：
编译期/单测期静默通过，**运行期才 1064**。
⇒ 必须**手工**改写全部 4 处，并**在 CI 中加一条静态检查**：用 MySQL 方言编译全部 ORM 语句，
断言不出现 `RETURNING`。

### 8.4 Partial Unique Index（**3 处，只有 2 处需要生成列**）

**其中 2 处**可以精确等价地映射为生成列唯一索引（因为 MySQL 唯一索引允许任意多个 `NULL`）；
**第 3 处（OAuth）不需要任何 workaround**（§4.4 已实测语义等价）：

```sql
-- ① uq_runs_thread_active（run/model.py:56-70）
active_thread_id VARCHAR(64)
  GENERATED ALWAYS AS (IF(status IN ('pending','running'), thread_id, NULL)) STORED,
UNIQUE KEY uq_runs_thread_active (active_thread_id)

-- ② uq_scheduled_task_run_active（scheduled_task_runs/model.py）
active_task_id VARCHAR(64)
  GENERATED ALWAYS AS (IF(status IN ('queued','launching','running'), task_id, NULL)) STORED,
UNIQUE KEY uk_scheduled_task_run_active (active_task_id)

-- ③ idx_users_oauth_identity（user/model.py:88-95）
-- 🔴 **不需要生成列、不需要 CONCAT、不需要分隔符。**
--    直接建全量唯一索引，语义与生产 PG 今天实际运行的索引完全一致（§4.4 已实测）
UNIQUE KEY idx_users_oauth_identity (oauth_provider, oauth_id)
```

**或对 ① ② 使用函数索引**（8.0.13+，可避免新增列）：

```sql
UNIQUE KEY uk_runs_thread_active (
  (IF(status IN ('pending','running'), thread_id, NULL)));
```

**二选一权衡**：生成列更可读（可加中文 COMMENT、可被 `SELECT` 引用）；
函数索引无需新增列。**推荐生成列**（可读性更好，且若执行 Compliance Pass 可加 COMMENT）。

> 🔴 **生成列只适用于 ① ② 两处**。**③ OAuth 已从该清单中移除** —— 见 §4.4 的两条证据。
>
> 🔴 **① 的唯一键名必须与 ORM 索引名逐字一致（`uq_runs_thread_active`）**：
> `RunManager` 把 MySQL 的 `Duplicate entry … for key 'runs.uq_runs_thread_active'`
> 映射为既有的 409 overlap contract（`runtime/runs/manager.py::_is_active_run_conflict`，
> 按 key 名判别，避免把所有 1062 都当成 run conflict）。**改名必须同时改判别函数**，
> 否则并发 admission 的败方会从 409 退化为 500。已实测（R2 blocker 2）。
>
> ⚠️ **① ② 的生成列写法**：生成列的值必须是"参与唯一性判断的键"，
> 且 `ELSE NULL` 分支保证"非活跃行不参与约束"。**已实测**：
> 同一 thread 上 3 个终态 run + 1 个活跃 run 共存（4 行），
> 再插第二个活跃 run → `ERROR 1062 Duplicate entry 't1' for key '…uq_runs_active_per_thread'`。
>
> ⚠️ **`idx_users_oauth_identity` 的额外风险不在索引，在错误判别**：
> MySQL 1062 也带 key 名，但**错误对象形状不同**、且**消息里不含列名**，
> ⇒ 三个判别函数会**全部返回 `False`**，冲突退化为 500。**修法见 §4.9-D。**

### 8.5 SKIP LOCKED

- ✅ 8.0.1+ 可用；实测 SQLAlchemy 编译为 `… LIMIT %s FOR UPDATE SKIP LOCKED`。
- ⚠️ `SKIP LOCKED` 不能与 `NOWAIT` 同用；在 `UNION` / 聚合子查询中不可用
  （现有语句都是单表 `SELECT`，OK）。
- ⚠️ 无索引时 `SKIP LOCKED` 仍会扫全表（只是跳过被锁行）→ 性能退化而非错误。

**当前只有 2 处**（`scheduled_tasks/sql.py:332,530`，due-task claim）——
其余用法已随 `mcp_tasks` / `subagent_batches` 删除（§2.3）。

### 8.6 `run_events.seq` 的分配语义

#### 现状代码的三个事实

```python
# runtime/events/store/db.py:133-153
@staticmethod
async def _max_seq_for_thread(session, thread_id) -> int | None:
    stmt = select(func.max(RunEventRow.seq)).where(RunEventRow.thread_id == thread_id)
    dialect_name = session.get_bind().dialect.name

    if dialect_name == "postgresql":
        await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(...))"), ...)
        return await session.scalar(stmt)

    return await session.scalar(stmt.with_for_update())     # ← MySQL 走这里
```

| # | 事实 |
| --- | --- |
| 1 | **只有 `postgresql` 有专属分支**；`else` 覆盖其余所有方言，包括 MySQL |
| 2 | `else` 分支的正确性来源是 **SQLite**：SQLite 方言**忽略** `FOR UPDATE`（渲染为空），其串行化来自"整库单写者锁" —— **与 `FOR UPDATE` 无关**（`db.py:137-141` 的 docstring 写着 "Other dialects keep the existing row-locking statement"） |
| 3 | 单进程内的竞态由**进程内 `asyncio.Lock`** 兜住（per `thread_id`，`db.py:32-45`）；DB 层语句**只对跨进程写者有意义** |

**真正的问题只有一个**：`else` 分支在 **MySQL + 跨进程**下是否仍能串行化。
`run_events` 已有 `uq_events_thread_seq(thread_id, seq)`，**唯一约束会兜住重复**，
但 `runtime/events/` 全目录**没有任何 `IntegrityError` 处理**，
所以"兜住"的实际表现是**整个 batch 事务失败**，而不是"重试一次就好"。

> ⚠️ **这与 `RETURNING` 陷阱是同一类错误**：
> `SELECT max(…) … FOR UPDATE` 在 MySQL 上**不报错、能跑通、单测能过、小数据量下永远看不出问题**，
> 只在"多 Worker + 同一 thread 并发写事件"时表现为序号冲突或事务失败。
> **"编译通过" ≠ "运行可用" ≠ "具备串行化语义"。**

#### 🔴 V3 实测结果（**结论是负面的**）

**测试方法**：在真实 `mysql:8.0.24` 容器内，用**两个独立会话**、
`SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED`，
**忠实复现生产语句**（即"用 `FOR UPDATE` 读到的 max 去算下一个 seq"，而不是额外再读一次）。

| 用例 | 语句 | 结果 |
| --- | --- | --- |
| **V3-c（核心）** | A：`SELECT IFNULL(MAX(seq),0) INTO @m … FOR UPDATE` → `SLEEP(4)` → `INSERT … VALUES (@m+1)`；B：1 秒后执行同一序列 | 🔴 **两者都读到 `@m = 0`**。B 先插入 `seq=1` 成功；**A 插入时 `ERROR 1062 Duplicate entry 't1-1' for key 'run_events.uq_events_thread_seq'`** |
| V3-b（空 thread） | 同上，`run_events` 对该 thread 无任何行 | 🔴 **确认"无行可锁"**：`MAX` 为 `NULL` → 双方都从 `seq=1` 开始 → 同样 1062 |
| 候选替代方案 | 单语句 `INSERT INTO run_events (…) SELECT 't1', …, IFNULL(MAX(seq),0)+1 FROM run_events WHERE thread_id='t1'` | 🔴 **同样失败**：后写者在索引项上与未提交行冲突后等待，等对方提交即报 `1062` |

**⇒ 结论（V3 判定）**：

> 🔴 **`SELECT MAX(seq) … FOR UPDATE` 在 MySQL 8.0.24 + READ COMMITTED 下
> 不提供任何跨事务串行化**；把"读 max"合并进单条 `INSERT … SELECT` 也**不解决问题**。
> 这与 PostgreSQL 的 `pg_advisory_xact_lock` **不是等价物**。
>
> 后果的严重性来自另一个事实：`runtime/events/` 全目录**没有任何 `IntegrityError` 处理**
> ⇒ 一次竞态 = **整个 batch 事务失败**（事件丢失），不是"重试一次就好"。

#### ✅ 已实测可行的修法：**先锁一行真实存在的行，再读 max**

> 🔴 **锚点表的真实名称是 `threads_meta`（不是 `threads`）。**
> 依据 ORM 实测：`persistence/thread_meta/model.py:13-16` ⇒ `__tablename__ = "threads_meta"`，
> **主键 `thread_id: String(64)`**。仓库里**不存在**名为 `threads` 的表
> —— `SELECT id FROM threads ...` 是**错误的，不得作为实施方案**。

**做法**：在 `_max_seq_for_thread` 里，把"对聚合结果 `FOR UPDATE`"换成
"**对一行真实存在的 `threads_meta` 行取行锁**"：

```python
# runtime/events/store/db.py —— 新增 mysql 分支
if dialect_name == "mysql":
    # ① 确保锚点行存在（幂等，不覆盖既有字段）—— 见下方"锚点存在性"说明
    await session.execute(
        text(
            "INSERT INTO threads_meta (thread_id, status, metadata_json, created_at, updated_at) "
            "VALUES (:thread_id, 'idle', '{}', :now, :now) "
            "ON DUPLICATE KEY UPDATE thread_id = thread_id"
        ),
        {"thread_id": thread_id, "now": now},
    )
    # ② 对锚点行取 X 锁（此时它必然存在）⇒ 同一 thread 的并发写者被真正串行化
    await session.execute(
        text("SELECT thread_id FROM threads_meta WHERE thread_id = :thread_id FOR UPDATE"),
        {"thread_id": thread_id},
    )
    # ③ 拿到行锁之后才读 max
    return await session.scalar(stmt)

if dialect_name == "postgresql":
    ...   # 保持 pg_advisory_xact_lock 不变

return await session.scalar(stmt.with_for_update())   # sqlite 等
```

##### 🔴 锚点存在性：**不能假设 `threads_meta` 行一定先建过**

"能写事件的 thread 一定先建过锚点行"这一假设**不成立**：

| 事实 | 证据 |
| --- | --- |
| 线程元数据由 `_ensure_thread_metadata()` 在 **run admission 阶段**创建 | `app/gateway/services.py:190-226`（`thread_store.create(...)`） |
| **但它失败是"非致命"的** —— 默认路径 `require_existing_thread=False`，异常只记一条 warning，随后**继续走 `run_agent`** | `services.py:1628-1633`（日志带 `(non-fatal)`）· `services.py:1662-1665`（注释明确写 *"Continue through run_agent even after metadata abort, timeout, or strict verification failure"*） |
| 且当前**没有任何生产调用点传 `require_existing_thread=True`** | 全仓扫描：该参数只在 `services.py` 内部流转，无调用方置真 |

⇒ **"metadata 创建失败/超时/被 abort"与"事件写入"可以同时发生**，
锁锚点必须在事件路径上**自保证存在**，否则 `SELECT ... FOR UPDATE` 会锁到 0 行
（不报错、也不串行化）—— 那等于**没有修**。

> ⚠️ 步骤 ① 的幂等 upsert 本身会对该行取 X 锁，**理论上已足以串行化**；
> 步骤 ② 的 `SELECT … FOR UPDATE` 是显式锚点 + 可读性保障。
> ⛔ **实现时需确认它与应用侧 `threads_meta` 写入（`SqlThreadMetaStore.create/update_owner/set_project`）
> 不存在死锁环** —— 这属于 Goal 3 的实施细节。
> ⛔ 幂等 upsert **不得覆盖既有字段**（`ON DUPLICATE KEY UPDATE thread_id = thread_id` 是 no-op 写法）。

**实测验证（同一容器、同样的并发脚本）**：

| 会话 | 行为 | 结果 |
| --- | --- | --- |
| A | 锁锚点行 → 读到 `max=0` → `SLEEP(4)` → 插 `seq=1` | ✅ 成功 |
| B（1 秒后） | **在锚点行上阻塞约 3 秒** → 解锁后读到 **`max=1`** → 插 `seq=2` | ✅ 成功 |

⇒ **两个写者都成功，序号连续且不重复，没有任何 1062。**
**修法只需要"改锁对象"，不新增表、不新增 abstraction。**

**为什么不选其它方案**：

| 方案 | 评价 |
| --- | --- |
| `GET_LOCK('deerflow:run_events:<thread_id>')` | ⚠️ **已实测可用**（返回 1），但它是**连接级**、必须显式释放、且要求"获取与释放锁在同一条连接上" ⇒ 与连接池天然冲突，**不作为业务锁方案** |
| **新增 per-thread counter / sentinel 表** | ⚠️ 可行，但**需要新表 + 处理锚点创建竞态**。而 `threads_meta` 行**语义完全匹配**（一个 thread 一行）⇒ **优先复用它**。⚠️ 但它**不保证存在** ⇒ 必须配合上面的幂等 upsert |
| 唯一约束 + bounded retry | ⚠️ 可作为**兜底**（防御性），但需要新写重试代码，且高并发下会变成重试风暴。**建议只作保险，不作主方案** |

**明确不采用**：把 `seq` 交给 Redis 分配（序号是**排序真相源**，volatile Redis 丢失会跳号/重号）。

> 🔴 **本条是"方案已定的实施项"** —— 它进入 **Goal 3**，并带一条**回归验收**：
> 制造"同一 thread 并发写事件"的竞态，确认**不再出现 1062 且序号连续**（Goal 4）。

### 8.7 `FOR UPDATE` 的索引核验（28 处）

| 文件 | 处数 | 需要核验的索引 |
| --- | --- | --- |
| `persistence/scheduled_tasks/sql.py` | **11** | `ix_scheduled_tasks_next_run_at`（存在）、`ix_scheduled_tasks_status`（存在） |
| `persistence/scheduled_task_runs/sql.py` | **8** | `ix_scheduled_task_runs_status`；⚠️ **`(status, created_at)` 复合索引缺失**（FIFO claim 的 `ORDER BY attempt_count, created_at, id`） |
| `persistence/thread_meta/sql.py` | **6** | `ix_threads_meta_user_id` / `_assistant_id` / `_project_id` |
| `runtime/events/store/db.py` | 1 | `uq_events_thread_seq(thread_id, seq)` |
| `persistence/run/sql.py` | 1 | `ix_runs_lease(lease_expires_at)` |
| `persistence/projects/sql.py` | 1 | `ix_projects_user_id` / `_status` |
| **合计** | **28** | |

> **统计口径说明**：本表的"处数"是**包含 `with_for_update` 的行数**，
> 既含方法链 `.with_for_update(...)`，也含关键字参数
> `session.get(Row, id, with_for_update=True)` 形式。
> 后者在 `scheduled_task_runs/sql.py` 有 8 处，
> 因此只按方法链 grep 会漏计。

> **风险**：MySQL 在无法使用索引时会锁定扫描到的**所有行**。
> 必须逐一核对每个 `with_for_update` 语句都有可用索引；
> 上面标 ⚠️ 的 `scheduled_task_runs` 需要新增复合索引（§9.2）。

**RR 下的 gap lock**：`SELECT … FOR UPDATE` 在 RR 下对范围扫描加间隙锁，
会**改变并发插入行为**。`scheduled_task_runs/sql.py` 的 `claim_queued_run` 依赖
`WHERE id = ? AND status = 'queued' AND ~older_same_thread` 的条件 UPDATE 返回
`rowcount == 1` 判定抢占成功；RR 下该语句的锁行为与 RC 不同。

**锁顺序纪律**（RR 下需重新验证）：
`scheduled_task_runs/sql.py:754-757`（task → scheduled-run）、`scheduled_tasks/sql.py:680-684`。

### 8.8 时间语义（一个容易漏掉的破坏点）

| 问题 | 证据 | 后果 |
| --- | --- | --- |
| MySQL `DATETIME` 默认精度为 0 | 实测：`DateTime(timezone=True)` 在 MySQL 方言下编译为 **`DATETIME`**（PG 是 `TIMESTAMP WITH TIME ZONE`） | **亚秒被截断**。`scheduled_task_runs` 的 FIFO 排序是 `order_by(created_at.asc(), id.asc())`，`expire_queued_runs` 用 Python 计算带微秒的 `created_before` 比较 → 截断后**排序与过期判定都会错** |
| `DateTime(timezone=True)` 在 MySQL 上**静默丢弃时区** | MySQL 无时区感知的 `DATETIME` | 只要全部写入都是 `datetime.now(UTC)`（offset 0），wall clock 恰好等于 UTC，**"碰巧正确"**。一旦有任何非 UTC 写入即静默错误 |
| 读回是 naive datetime | SQLite 已有此问题（`events/store/db.py` 有注释） | 代码里已有 `coerce_iso` 与 `_lease_is_alive` 的 `replace(tzinfo=UTC)` 兜底，但**不是所有路径都覆盖**，需要系统审计 |
| `TIMESTAMP` 不可用 | 2038 上限 + 隐式时区换算 | 必须用 `DATETIME(6)`，不能用 `TIMESTAMP` |
| 全库影响面 | **29 个** `DateTime(timezone=True)` 列 | 需要逐列改为 `mysql.DATETIME(fsp=6)` |

**建议**：所有时间列显式改为 `mysql.DATETIME(fsp=6)`，并在应用侧统一
"UTC aware → naive UTC" 的写入边界转换。

### 8.9 必须显式验证的并发场景

**场景 A：同一 `thread_id` 的两个 Agent Run 并发写 Checkpoint**

- PG 行为：`checkpoints` 主键冲突 → `ON CONFLICT DO UPDATE`；`checkpoint_blobs` 冲突 → `DO NOTHING`。
  并发写**不报错**，后写覆盖。
- MySQL 行为：第三方包用 `ON DUPLICATE KEY UPDATE`（`base.py:223-238`），语义相近。
  **但**：RR 隔离下两个事务同时对同一 PK 做 upsert 会先取锁 → 后到者等待；
  若两者还持有其它行锁，可能死锁（MySQL 抛 `1213` 并 kill 一个事务）。
- **额外的语义变化**：现有保护是 `uq_runs_thread_active`（每线程至多一个活跃 run），
  因此 checkpoint 并发写的**正常路径是串行的**。该约束在 MySQL 上通过生成列唯一索引实现后
  **唯一性语义等价**，但错误码从 `23505` 变 `1062`，
  `runtime/runs/store/base.py` 的 `IntegrityError` 判定路径需要复核。

**场景 B：两个 Worker 抢同一队列行（Scheduler）**

- PG：`SELECT … FOR UPDATE SKIP LOCKED` → 后到者跳过被锁行，取下一行。
- MySQL：`SKIP LOCKED` 支持，语义一致。
  **但**：RR 隔离下未命中的行会加 gap lock，导致"跳过"行为在间隙上不完全等价；
  且 `innodb_lock_wait_timeout` 默认 **50 秒**。
- 结论：**必须做并发压测**（`scripts/benchmark/concurrency/worker.py` 可作为起点）。

---

## 9. 索引设计

### 9.1 索引集合（31 个）

**命名规范改造属于 Optional Compliance Pass**（§2.7）。规范内容：

> 所有 index / constraint 名 ≤ 100 字符；unique → `uk_<field_name>`；
> 普通 → `idx_<field_name>`；composite 使用简短可读名。

**若执行 Compliance Pass，改规则而非逐条列**（31 个索引）：

| 现前缀 | 数量 | 目标前缀 |
| --- | --- | --- |
| `ix_` | 约 27 | `idx_`（其中 unique 语义的改为 `uk_`） |
| `idx_` | 1 | 保持 `idx_`（`idx_users_oauth_identity` 是 unique，改为 `uk_users_oauth_identity`） |
| `uq_` | 3 | `uk_` |

**必须改名为 `uk_` 的**（unique 语义）：
`ix_personal_access_tokens_token_digest`、`uq_runs_thread_active`、`uq_runs_idempotency_key`、
`uq_scheduled_task_run_occurrence_seq`、`uq_scheduled_task_run_active`、
`ix_users_email`、`idx_users_oauth_identity`。

**未命名唯一约束**：`managed_subagents.name` 必须补名（建议 `uk_managed_subagents_name`）。

**长度核验**：最长目标名 `uk_personal_access_tokens_token_digest`（39 字符）
与 `uk_scheduled_task_run_occurrence_seq`（38 字符），全部 ≤ 100，安全。

### 9.2 必须新增的索引（**能力补偿，非优化**）

| 目标索引 | 表 | 理由（**不建它会失去什么正确性/能力**） |
| --- | --- | --- |
| `idx_scheduled_task_runs_status_created` | `scheduled_task_runs` | occurrence 队列 claim 的 `WHERE status='queued' ORDER BY attempt_count, created_at, id`（现有只建了活跃去重索引）—— **不建则 claim 退化为全表扫描 + 锁全表** |
| `uk_users_oauth_identity`（**全量唯一索引，非生成列**） | `users` | partial unique index 的 MySQL 等价实现（§4.4 已实测语义等价）—— **不建则失去"每个 OAuth 身份一个账号"的唯一性保证**。⚠️ **不需要生成列** |

> ⚠️ 本清单**必须逐条回答"不建它，哪条 SQL 会失去正确性保证"**；
> **答不上来的一律不做** —— 它属于"性能优化"，归入 §9.4 的不做清单。

### 9.3 必须保留的查询覆盖（不得因去重而破坏）

| 查询模式 | 位置 | 依赖索引 |
| --- | --- | --- |
| 每线程一个活跃 run（唯一性 + reject 策略） | `runtime/runs/manager.py`、`runtime/runs/store/base.py` | `uk_runs_thread_active`（生成列） |
| 租约过期扫描 | `persistence/run/sql.py`、`scheduled_task_runs/sql.py:569-572` | `idx_runs_lease` |
| 事件流分页（`thread_id` + `category` + `seq`） | `runtime/events/store/db.py` | `idx_events_thread_cat_seq`（leftmost prefix 覆盖 `thread_id`） |
| 事件序号唯一性 | 同上 | `uk_events_thread_seq` |
| 线程列表按用户 + 助手过滤 | `persistence/thread_meta/sql.py` | `idx_threads_meta_user_id`、`idx_threads_meta_assistant_id` |
| 线程置顶 / 归档排序与过滤 | `persistence/thread_meta/sql.py:230,247,261` | `json_match` 走 `metadata_json` 的 JSON 路径（**无专用索引**）⇒ ⚠️ 见下 |
| due-task claim（`next_run_at` + lease 过期） | `scheduled_tasks/sql.py:320-332` | `idx_scheduled_tasks_next_run_at` ✅ 已存在 |
| occurrence 队列 claim（`status='queued'` + `created_at`） | `scheduled_task_runs/sql.py:270-285` | ⚠️ **需新增**（§9.2） |
| OAuth identity 唯一性 | `app/gateway/auth/` | `uk_users_oauth_identity`（全量唯一索引） |
| 偏好逐 key 读写 | `persistence/user/preferences.py` | PK `(user_id, key)` |

> ⚠️ **线程置顶/归档过滤是一个已知的索引缺口**：它走 `JSON_EXTRACT`，
> **无法使用普通 B-Tree 索引**。可选缓解（**均属性能优化，不在本次迁移范围**）：
> ① 在 `metadata_json` 上建**函数索引**；
> ② 拆 scalar 列。**先保持现状**，上线后用真实慢查询判断。

### 9.4 不删除任何索引；不做任何无关索引优化

**结论：本次迁移不主动删除任何现有索引（除 Compliance Pass 的改名外）。**

理由是 MySQL 的锁行为对索引缺失更敏感（§8.7，"无索引 = 锁全表"），
删除索引的风险高于收益。若确需去重，应在 MySQL 上跑 `EXPLAIN` + 锁范围实测后再决定。

**明确不做的事**：

| 不做的事 | 为什么不做 | 什么时候做 |
| --- | --- | --- |
| **冗余索引清理** | 需要**真实 workload** 才能判断"哪个索引没人用"；迁移期删索引会把"迁移引入的问题"与"索引变更引入的问题"混在一起，**失败归因不可能** | MySQL 上线后，用 `performance_schema.table_io_waits_summary_by_index_usage` + 真实负载 |
| **query tuning** | 同上；且 **PG 与 MySQL 的优化器行为不同，PG 上的结论不可迁移** | 上线后按 `EXPLAIN ANALYZE` |
| **索引合并**（多个单列索引 → 复合索引） | 会改变 `with_for_update` 的**锁范围** —— 这是**并发语义**，不是性能问题 | 上线后单独评估，且**必须配并发回归测试** |
| **根据推测 workload 新增优化索引** | "推测的 workload"在数据库替换场景里几乎总是错的 | 上线后按真实慢查询 |
| 覆盖索引 / 索引下推调优 | 同上 | 同上 |
| **为 JSON 过滤拆 scalar 列 / 建函数索引** | 无正确性依据 | 上线后按真实慢查询（§9.3） |

**只做两类索引动作**（其余一律不做）：

1. **等价迁移**：现有 **31 个**索引按现有命名重建（**索引集合不变**）；
2. **能力补偿**：为 MySQL 缺失的能力补索引 —— **2 处** partial unique 的生成列替代（§8.4），
   以及 occurrence claim 的复合索引（§9.2）。这两类是**正确性必需**，**不是优化**。

---

## 10. 序列化与对象存储

### 10.1 应用侧序列化现状

| 位置 | 方式 |
| --- | --- |
| `persistence/engine.py:25-27` | `json.dumps(obj, ensure_ascii=False)`（`json_serializer`） |
| `runtime/events/store/db.py` `_content_to_db` | 非字符串内容 `json.dumps(default=str, ensure_ascii=False)` + metadata 标记 `content_is_json`；读回时按标记 `json.loads` |
| `persistence/agents/file.py` / `managed_subagents/file.py` | 文件型存储（JSON 文档） |
| `agents/memory/backends/deermem/` | 文件（`memory.json` manifest）+ SQLite FTS5 |

> 这段同时也是"`sa.JSON` 在 MySQL 原生 `JSON` 上继续可用"的佐证 ——
> 应用侧本来就把 JSON 当文档整体读写，不做服务端运算。

### 10.2 Checkpoint payload **不接入** Object Storage

| 项 | 结论 |
| --- | --- |
| Checkpoint blob 的存储 | **`LONGBLOB` 单库直存**（§7.6） |
| `inline_blob_threshold` 配置项 | ❌ 不引入 |
| `blob_ref` / `blob_size` / `blob_sha256` 列 | ❌ 不引入 |
| 孤儿对象 GC | ❌ 不实现 |
| 5 步写入顺序协议 | ❌ 不实现 |
| **唯一需要落实的** | ⚠️ 部署 checklist 显式确认 `max_allowed_packet`；采集 blob 长度分布作为后续决策依据 |

**理由**：跨存储最终一致性是本设计里最贵的复杂度，而它解决的是**尚未发生的容量问题**。
MySQL `LONGBLOB` 上限 4 GB，远超现实 payload。

### 10.3 已存在的 Object Storage 用途（**本次不动**）

| 数据 | 现状 | 处置 |
| --- | --- | --- |
| Tool 结果大文本 | ✅ 已卸载（阈值 12000 字符），**已走对象存储** | 保持 |
| Uploads / 附件 | 单文件上限 50MB，**已走对象存储** | 保持 |
| Agent workspace 变更 / thread outputs | **已走对象存储** | 保持 |
| 用户 custom skills | **已走对象存储**（Phase 5） | 保持 |
| `runs.first_human_message` / `last_ai_message` | DB `TEXT` | 保留（列表页需要，且已截断语义） |

> 端口已就绪：`object_storage/port.py`（`write_stream` / `open_read` / `stat` /
> `list_prefix` / `delete` / `copy`，aiobotocore 流式 multipart）；
> `keys.py` 定义了 `users/{user_id}/threads/{thread_id}/outputs|uploads|.tool-results`
> 与 `users/{user_id}/skills/custom` 的稳定命名空间。

---

## 11. 迁移链、Schema 产物与 Runtime 校验

### 11.0 生产执行模型总览

**把"建 Schema"从 Runtime 中整体移出。**
现状中 Runtime 的启动路径**同时承担了"建 Schema"与"用 Schema"两件事**：
`deps.py:432` 的 `init_engine_from_config()` → `init_engine()` →
`CreateSchema` / `_auto_create_postgres_db` / `bootstrap_schema()`（`create_all` + `stamp` + `upgrade`），
以及 checkpointer 分支里的 `await saver.setup()`。

#### 权限模型

| 账号 | 权限 | 用途 |
| --- | --- | --- |
| **生产 Application DB 账号**（Runtime 使用） | `SELECT` / `INSERT` / `UPDATE` / `DELETE` —— **无任何 DDL** | 进程启动 + 业务读写 |
| **迁移账号**（运维 / DBA 使用） | `CREATE` / `ALTER` / `DROP` / `INDEX` / `REFERENCES` 等 | 执行 Application Schema 与 Checkpoint Schema 迁移 |

⇒ **生产 Runtime 的启动路径中不允许出现任何 DDL**：
`CREATE DATABASE` / `CREATE SCHEMA` / `CREATE TABLE` / `ALTER TABLE` / `DROP TABLE` /
`CREATE INDEX` / `stamp` / `upgrade` / `create_all` / `setup()` —— **一个都不允许**。

#### 三份职责，三个执行方

| 职责 | 执行方 | 产物 | 位置 |
| --- | --- | --- | --- |
| **Application Schema 迁移** | 运维 / DBA | MySQL 独立 Alembic 链的 `upgrade head` | `persistence/migrations_mysql/`（§11.3） |
| **Checkpoint Schema 迁移** | 运维 / DBA | 由上游 `MIGRATIONS` 派生的**线性、版本化 SQL 产物** | `database/mysql/checkpoint/`（§11.4） |
| **Schema 存在性 + 版本校验** | **Runtime** | **只读**（`SELECT` / `information_schema`） | §11.5 |
| **Dev / CI / Compose 的自动建库建表** | 测试 / 开发流程 | 直接调 Alembic 与 `setup()` | §11.6 |

#### 生产发布流程（顺序不可颠倒）

```
① DBA / Migrator：执行 Application Schema 迁移    （alembic upgrade head）
② DBA / Migrator：执行 Checkpoint Schema 迁移      （database/mysql/checkpoint/ 线性脚本）
③ 校验：Application revision 满足要求
        + checkpoint 4 张表存在
        + checkpoint_migrations 版本满足当前 Saver
④ 部署应用：Runtime 启动 → 只做只读校验 → readiness = true
```

> 🔴 **Checkpoint 迁移产物与 Saver 版本绑定**：产物由 `langgraph-checkpoint-mysql==3.0.0`
> 的 `MIGRATIONS` 派生 ⇒ **升级 Saver 版本必须重走一遍 ①–④**（§11.4.4）。
> ⛔ **禁止 Runtime 自动升级 Checkpoint Schema。**

> 🔴 **为什么用"调用路径隔离"而不是 `auto_setup=true/false` 开关**：
> 一个布尔开关**极易配错**（配置漂移、环境差异、复制粘贴），
> 而它配错的方向恰好是"**生产环境悄悄执行了 DDL**"这种最危险的失败。
> 改用**调用路径隔离**后，生产路径上**根本不存在能执行 DDL 的代码** ——
> 错误配置无法制造出一个不存在的代码路径。**这是结构性保证，不是配置纪律。**

### 11.1 为什么必须"独立链"，而不是"PG 链 + 新 root"

| 方案 | 问题 |
| --- | --- |
| A. 单链 + 新增一个 baseline root | **一条 Alembic 链只能有一个 root**。旧链的 `0001_baseline` 已是 root，再加一个 root ⇒ **multiple heads**；`ScriptDirectory.get_current_head()` 在多 head 时**抛 `CommandError`**，而 `bootstrap.py::_get_head_revision()` 会把它包成 `RuntimeError` ⇒ **启动期直接失败**。要规避只能插"假的链边 revision"，属于自造机制，且把新库的起点永久绑在旧库的历史上 |
| B. **独立链（采用）** | 两条链各有自己的 `script_location` 与 `version_table`，`alembic heads` 在**各自链内**只有一个 head。**已有先例**（`migrations/AGENTS.md:128` 已确立 `version_table="<prefix>alembic_version"` 的用法），**无需新机制** |

**PG 链的处置**：`0001_baseline` … `0023_user_preferences`（24 个文件）
视为 **immutable**，**保持原样、不修改**，只作为**旧实现历史**存在；
MySQL 侧**不引用、不重放**。它们的退役时点是 **Goal 5**（随 PG backend 一并删除）。

**MySQL 链的起点**：**`0001_mysql_baseline`** —— 它是 **MySQL 自己的链根**，
由裁剪后的最终 Schema 生成（**12 张应用表 / 139 列**），**从不创建任何已删除的对象**。

> 🔴 **checkpoint 的 4 张框架表不在 MySQL 链内** —— 这是**刻意的**：
> 它们的 DDL 由**上游 Saver 的 `MIGRATIONS`** 定义，**不由本项目重新发明**（§11.4）。
> 生产环境中它们由**运维 / DBA 执行独立的迁移产物**创建。
> `migrations/_env_filters.py:30-37` 的 `LANGGRAPH_OWNED_TABLES`
> **已含这 4 个名字** ⇒ Alembic 自动回避，**默认不需要改动**。
> ⚠️ 但 `backend/tests/test_persistence_migrations_env.py:60-63` 对该集合有**等值断言**
> ⇒ **若 §6.4 决定改名，这个断言必须同步改**。

### 11.2 双链隔离的接线点（逐处核对）

> 🔻 **原则**：**不构建"通用 multi-chain migration framework"**。
> 只做"backend 明确选择对应 script location"这一件事。

| 接线点 | 现状（PG 单链） | MySQL 侧需要的改动 |
| --- | --- | --- |
| `script_location` | `_MIGRATIONS_DIR` —— **单目录** | MySQL 需要**第二个脚本目录**（如 `persistence/migrations_mysql/`）。**backend 选择哪个目录**由 `backend` 参数决定，**不需要注册表 / 插件机制** |
| `version_table` | **未设置** ⇒ 默认 `alembic_version` | ✅ **最简单方案：继续用默认 `alembic_version`**（两条链在不同 database 中，天然隔离）。仅当组织要求 `ag_` 前缀时才设 `ag_alembic_version` |
| head / known-revision 缓存 | `_HEAD_REVISION` / `_KNOWN_REVISIONS` 是**模块级单例**；两个 getter 都只认**一个** `script_location` | 🔴 **从生产路径上直接删除，不做"按链做键"的缓存抽象** —— 见下方说明 |
| `_get_head_revision()` 的失败语义 | `get_current_head()` 返回 `None` 时抛 `RuntimeError` | 语义保持，但必须确保它读的是**本链**的 head |
| `_get_alembic_config(engine, *, postgres_schema="")` | 注入 `script_location` + `sqlalchemy.url` + 可选 `deerflow_pg_schema` | MySQL 分支注入**本链** `script_location`；**不带** `deerflow_pg_schema` |
| `migrations/env.py` | 读 `deerflow_pg_schema`、调 asyncpg 专用 connect args、`render_as_batch=True`（注释明写 "Required for SQLite ALTER TABLE support"）、SQLite `PRAGMA busy_timeout` 钩子 | **这些对 MySQL 全都不适用**。MySQL 链需要**自己的 `env.py`**（一个精简文件，约 40 行），**不复用 PG 的注入逻辑**，也**不抽公共基类** |
| `_read_database_revision(conn)` | `SELECT version_num FROM alembic_version`，且要求**恰好一行** | 表名随 `version_table` 的决策；"恰好一行"的断言**保留**（它是防串链的有效检查） |

> 🔴 **关于 `_HEAD_REVISION` / `_KNOWN_REVISIONS`**：
> **不需要**把它们"按 `script_location` 做键" —— 理由直接来自新执行模型本身：
>
> | 事实 | 推论 |
> | --- | --- |
> | 它们只在**执行 DDL 的路径**上被消费（`bootstrap_schema()` / `_get_revision_metadata()`） | 该路径**已整体移出 Runtime**（§11.0） |
> | `_KNOWN_REVISIONS` 只被 **`legacy` 分支**使用 | MySQL 的 `legacy` 状态**直接拒绝启动**（§11.3）⇒ MySQL 侧不需要它 |
> | 删除 `legacy` / forward-compatible 分支后，**唯一还需要 head 的是测试** | 生产路径不再需要任何缓存 |
>
> ⇒ **决策：把这两个模块级缓存从生产路径上删掉**，
> 而**不是**为"短暂的双链期"新增一层**按链做键的缓存抽象**。
> 这直接对应原则：**"如果缓存没有明确价值，优先取消缓存。"**
> ⚠️ 若 `_get_head_revision()` 仍被测试使用，可保留为**无缓存的即时计算**。
>
> 📌 **PG 删除后（Goal 5）自然只剩一条 MySQL 链** ⇒ 双链期是**短暂的**。

### 11.3 Application Schema 的迁移产物（**由运维 / DBA 执行**）

**执行方：运维 / DBA（高权限迁移账号）。Runtime 不执行。**

现状的 DDL 入口（必须逐处拆开）：

| 位置 | 现在做什么 | 处置 |
| --- | --- | --- |
| `deps.py:432` `init_engine_from_config(config.database)` | Gateway lifespan 里调用 ⇒ **生产 Runtime 的 DDL 入口** | 🔴 **拆成两半**：保留"建 engine + session factory"，**移出"建 Schema"** |
| `engine.py:199-212` `CreateSchema(postgres_schema, if_not_exists=True)` | 建 PG schema | ⛔ **MySQL 侧不实现**（MySQL 无独立 schema 概念） |
| `engine.py:58-82` `_auto_create_postgres_db(url)` | 连维护库跑 `CREATE DATABASE` | ⛔ **删除，且明确不移植到 MySQL**（§11.3.1） |
| `engine.py:213` `await bootstrap_schema(_engine, backend=…)` | `create_all` / `stamp` / `upgrade head` | ⛔ **从 Runtime 移除**，改为运维执行 `alembic upgrade head` |
| `engine.py:214-236` "does not exist" 时自动建库 + 重建 engine + 重跑 | 隐式权限需求 | ⛔ **整段删除** |
| `engine.py:132-168` SQLite 分支的 `os.makedirs` + WAL PRAGMA | dev 路径 | ⚠️ **保留**（dev / 单测仍需要），但**不属于生产路径** |

#### 11.3.1 不移植自动建库：库不存在就启动失败

| 方案 | 判定 |
| --- | --- |
| 移植成 `CREATE DATABASE IF NOT EXISTS` | ⛔ **不做** —— 它要求生产 Runtime 账号具备 `CREATE` 权限，与权限模型直接冲突（§11.0） |
| 由 Docker Compose 的 `MYSQL_DATABASE` 创建 | ✅ **dev / compose 走这条** |
| 由运维 / DBA 创建 | ✅ **生产走这条** |
| 库不存在时 Runtime 的行为 | 🔴 **启动失败**（明确报错；不静默降级、不自动补建） |

#### 11.3.2 迁移执行方式

| 环境 | 谁执行 | 命令 |
| --- | --- | --- |
| **生产** | 运维 / DBA（迁移账号） | 在 **MySQL 独立链**上 `alembic upgrade head` |
| dev / CI | 测试流程（§11.6） | 同上，可自动执行 |
| Docker Compose | 一次性 job / entrypoint | 同上 |

**迁移产物落点**：MySQL 独立链的脚本目录（如 `persistence/migrations_mysql/`），
配**自己的 `env.py`**（精简版，约 40 行；**不复用** PG 的注入逻辑，也**不抽公共基类**）。

> ⚠️ **`0001_mysql_baseline` 必须一次写全裁剪后的 Schema**
> （12 张表 / 139 列 + 31 个索引 + **2** 个生成列唯一索引 + 1 个 FK）。
> 它是一份**较大但一次性**的 revision，且是后续所有 MySQL revision 的基线 ——
> **写错就要改链根**，因此必须完整通过 §11.9 的验收。
>
> ✅ 它**不含任何 COMMENT 与 `ag_` 前缀**（若不做 Compliance Pass）。

#### 11.3.3 `bootstrap_schema()` 的处置

`bootstrap_schema()`（`bootstrap.py:603-701`）**是一个迁移执行器，不是校验器** ——
它的三个分支（`empty` → `create_all` + `stamp`；`legacy` → baseline + `stamp` + `upgrade`；
`versioned` → `upgrade head`）**全都在执行 DDL**。

⇒ 🔴 **生产路径不调用它。** 它随 PG backend 在 Goal 5 退役。
**Runtime 需要的是一个只读的校验原语**（§11.5），**而不是复用 `bootstrap_schema`**。

| 原 `bootstrap_schema` 的状态 | PG 现状 | MySQL 侧 |
| --- | --- | --- |
| `empty`（无版本表、无业务表） | `create_all` → `stamp head` | ⛔ **不在 Runtime 出现** —— 运维执行 `upgrade head` 后即为 `versioned` |
| `versioned`（有版本表） | `upgrade(head)` | ✅ **运维执行 `upgrade head`**；Runtime 只读 revision 判定（§11.5） |
| `legacy`（有业务表但**无**版本表） | `_run_baseline_create_all_sync` + `stamp` | 🔴 **直接拒绝启动（fail closed）** —— MySQL 不存在"Alembic 之前就存在"的部署，出现该状态只可能是**连错库或人为改库** |
| `forward-compatible`（滚动前向） | `_FORWARD_COMPATIBLE_REVISION` + `_validate_forward_schema()` | ✅ **不需要** —— 该机制为 PG 那条滚动前向兼容链而存在；MySQL 链是**从 `0001` 起的线性链** |

### 11.4 Checkpoint Schema 的迁移产物（**由运维 / DBA 执行**）

**执行方：运维 / DBA。Runtime 不创建、不升级、不调用 `setup()`。**

#### 11.4.1 🔴 DDL 内容必须来自上游的 `MIGRATIONS`，不得自行发明

上游 `langgraph-checkpoint-mysql==3.0.0` 的 `base.py:MIGRATIONS` 是 **22 条**（0-indexed 0–21），
**不是**一份"干净建表语句"，而是一条**线性演进链**。把 22 条逐条渲染后在
**真实 MySQL 8.0.24** 上执行，得到的**最终** schema 与"按直觉手写的建表语句"差异很大：

| 项 | 最终 schema（实测导出） | 手写会踩的坑 |
| --- | --- | --- |
| `checkpoints` PK | `(thread_id, checkpoint_ns_hash, checkpoint_id)` | 直觉会写成 `(thread_id, checkpoint_ns, checkpoint_id)` |
| `checkpoint_blobs` PK | `(thread_id, checkpoint_ns_hash, channel, version)` | 同上 |
| `checkpoint_writes` PK | `(thread_id, checkpoint_ns_hash, checkpoint_id, task_id, idx)` | 同上 |
| `checkpoint_ns_hash` | `binary(16) NOT NULL`，**是普通列**（不是生成列） | 直觉会写成生成列 —— 但第 19–21 条 `MODIFY` **把它降级成了普通列**（为绕开 MariaDB issue #51） |
| `checkpoint_ns` | `varchar(2000) NOT NULL DEFAULT ''` | 会写成 150 或 255 |
| `blob` | **`longblob`** | 会写成 `blob`（64 KB 上限）或 `mediumblob` |
| 索引 | `checkpoints_thread_id_idx` / `checkpoints_checkpoint_id_idx` / `checkpoint_blobs_thread_id_idx` / `checkpoint_writes_thread_id_idx` | 名字无 `ix_` / `idx_` 前缀 |
| `metadata` | `json NOT NULL DEFAULT (_latin1'{}')` | — |

⇒ 🔴 **结论：迁移产物必须由上游 `MIGRATIONS` 派生，不能手写"等价" DDL。**

#### 11.4.2 🔴 产物必须是"线性 + 版本表守卫"，**不是幂等脚本**

**实测**：把同 22 条语句**再跑一遍** → 第 51 行立刻失败：

```
ERROR 1061 (42000): Duplicate key name 'checkpoints_thread_id_idx'
```

原因：第 6–9 条是 `CREATE INDEX`（MySQL **不支持** `CREATE INDEX IF NOT EXISTS`），
第 13–18 条含 `DROP PRIMARY KEY` —— **这条链本质上只能顺序执行一次**。

⇒ "可独立执行"的含义必须写清：

| ❌ 错误理解 | ✅ 正确理解 |
| --- | --- |
| "脚本可以重复跑" | "脚本是**线性、有序、只跑一次**的；重复执行的保护来自 `checkpoint_migrations` 版本表" |

上游 `setup()` 的幂等性**正是**由版本表提供的
（`SELECT v FROM checkpoint_migrations ORDER BY v DESC LIMIT 1` → 只跑 `v+1` 之后的增量，
`aio_base.py:49-73`）—— **迁移产物必须复刻这个机制**。

#### 11.4.3 产物的形态与落点

```
database/mysql/checkpoint/
├── README.md            # 来源：langgraph-checkpoint-mysql==3.0.0 base.py:MIGRATIONS（MIT）
│                        # 执行方式 / 顺序 / 版本表语义 / 不可重复执行的原因
├── 0001_checkpoint_migrations.sql
├── 0002_checkpoints.sql
├── ...
├── 0022_modify_ns_hash_plain.sql
└── verify.sql           # 只读校验：4 张表存在 + checkpoint_migrations 版本
```

**关键要求**：

| # | 要求 |
| --- | --- |
| 1 | **内容逐条来自上游 `MIGRATIONS`** —— 不重写、不"优化"、不合并、**不改变顺序** |
| 2 | 每条执行后向 `checkpoint_migrations` 写入对应 `v`（与上游 `setup()` 完全一致） |
| 3 | **版本化**：产物带 `SAVER_VERSION`（当前 `3.0.0`）+ `MIGRATIONS_COUNT`（当前 **22**） |
| 4 | **可独立执行**：不依赖 Gateway 启动，不依赖应用 Runtime 生命周期 |
| 5 | **与 Alembic 完全解耦**：`checkpoint_migrations` 与 `alembic_version` 互不干扰 |

> ✅ **建议的产物生成方式**：用一个一次性的导出脚本，直接从上游包的 `MIGRATIONS` 常量
> 渲染出上面这组 `.sql` 文件（**已用该方法成功渲染并在真实 8.0.24 上执行**，见 §16 证据）。
> 这样**上游升级时重新生成即可**，不会出现"第二份手抄本漂移"。

> 📌 **V2（`checkpoint_ns` 真实最大长度）已关闭**：上游最终 schema 把
> `checkpoint_ns` 的**主键参与**换成了固定 16 字节的 `checkpoint_ns_hash`
> ⇒ **索引尺寸与 `checkpoint_ns` 长度无关**。
> 剩下的唯一限制是 `varchar(2000)` 这个列宽（超长会 `1406`），
> 而 DeerFlow 自身在顶层**始终传 `checkpoint_ns: ""`**（子图 ns 由 LangGraph 生成）。
> ⇒ **不需要实测。**

#### 11.4.4 🔴 产物与 Saver 版本绑定；升级流程固定

**Checkpoint migration artifact 必须与 `langgraph-checkpoint-mysql==3.0.0` 版本绑定** ——
因为 DDL 内容就是该版本 `base.py:MIGRATIONS` 的派生物（§11.4.1）。

以后升级 Saver 版本时，**顺序不可颠倒**：

```
① 升级 dependency（改 pyproject 的 == 版本号）
② review 新版本的 MIGRATIONS（逐条 diff，确认增量与破坏性变更）
③ 生成新的运维 migration artifact（重新渲染，不手改旧产物）
④ DBA 执行新产物
⑤ 再部署 Runtime（此时 Runtime 的只读校验基线已同步更新）
```

> 🔴 **禁止 Runtime 自动升级 Checkpoint Schema。**
> Runtime 只做只读校验（§11.5）；版本不匹配时 **fail closed**，**不调用 `setup()` 自动修复**。
>
> ⚠️ **两侧基线必须同时变**：`pyproject` 的版本号 与 `verify_checkpoint_schema` 的
> 期望版本（`len(MIGRATIONS) - 1`）**来自同一个包**，因此只要版本固定正确，二者天然一致。
> ⛔ **不要手工硬编码版本号**（例如写死 `21`），否则升级时必然漂移。

### 11.5 Runtime 的 Schema / 版本校验（**只读，fail closed**）

**这是 Runtime 在 Schema 上的唯一职责。**

#### 11.5.1 校验内容

启动期 / readiness 探针检查、**不修改任何 Schema**：

| # | 校验项 | 判定方式 |
| --- | --- | --- |
| 1 | **Application Alembic revision 满足本应用版本要求** | `SELECT version_num FROM alembic_version`（**要求恰好一行**）⇒ 与本版本要求的 revision 比对 |
| 2 | **`checkpoints` 表存在** | `information_schema.tables` |
| 3 | **`checkpoint_blobs` 表存在** | 同上 |
| 4 | **`checkpoint_writes` 表存在** | 同上 |
| 5 | **`checkpoint_migrations` 表存在** | 同上 |
| 6 | **Checkpoint 迁移版本满足当前 Saver 要求** | `SELECT MAX(v) FROM checkpoint_migrations` 必须 `== len(MIGRATIONS) - 1`（3.0.0 即 **21**） |

#### 11.5.2 失败语义

缺失或不匹配时：

- 🔴 **fail closed** / **`readiness = false`**；
- 给出**明确、可操作**的报错文案：

```
Database schema is missing or outdated.
Run the required database migration before starting this application version.
```

- ⛔ **应用不得自动修复、不得自动升级、不得自动建表。**

> ⚠️ **第 6 项必须从包内常量推导，不要硬编码 `21`** ——
> `MIGRATIONS` 的条目数会随上游版本变化（这也是 §7.4 要求 `==` 精确固定版本的原因之一）。
> 推导式：`required_version = len(MIGRATIONS) - 1`。
>
> ⚠️ **第 1 项的"恰好一行"断言必须保留** ——
> 它是防"连错链 / 串链"的有效检查（现有 `_read_database_revision` 已有此断言，`bootstrap.py:359-368`）。
>
> ✅ **这条校验本身就是 Goal 4 的验收项**：
> ① 未执行迁移 → **fail closed**；② 已执行迁移 → `ready`；
> ③ **Runtime 全程没有任何自动建表 / 改表**（可用 DDL 权限为空的账号直接验证）。

### 11.6 Dev / Test 与生产的隔离（**靠调用路径，不靠开关**）

| 环境 | 允许的 DDL 行为 | 实现方式 |
| --- | --- | --- |
| **生产 Runtime** | ⛔ **零 DDL** | 生产路径上**不存在** DDL 代码（§11.0） |
| **Dev / CI / 集成测试** | ✅ 可自动执行 Alembic、可显式调 `saver.setup()`、可创建临时库、可自动初始化 Schema | 由**测试夹具 / bootstrap 脚本**调用，**不经 Runtime 路径** |
| **Docker Compose** | ✅ `MYSQL_DATABASE` 建库 + 一次性迁移 job | compose 配置 |
| **生产迁移** | ✅ 由运维 / DBA 执行 | 迁移产物（§11.3 / §11.4） |

> 🔴 **明确不使用 `auto_setup=true/false` 这类开关**（理由见 §11.0）。
> 安全性的来源是**调用路径隔离**：三类执行方走**三条不同的代码路径**，
> 生产路径上根本没有 DDL 语句可执行。
> ⇒ **配置写错也无法制造出一个不存在的代码路径。**

### 11.7 legacy 清单：只服务旧 PostgreSQL bootstrap 的逻辑

| 符号 | 位置 | 为什么是 legacy | 退役时点 |
| --- | --- | --- | --- |
| `_CANONICAL_0019_SCHEMA_FLOOR` | `bootstrap.py:134-171` | 硬编码 `0019` 规范 schema 的"表 → 列"地板，供 `_validate_forward_schema()` 使用；**MySQL 链没有 `0019`，也没有滚动前向** | Goal 5 |
| `_BASELINE_TABLE_NAMES` | `:198-210` | 供 `_run_baseline_create_all_sync()` 在 legacy 库上重建 `0001` 的产物；MySQL 无 legacy 分支 | Goal 5 |
| `_BASELINE_INDEX_NAMES` | `:215-249` | 同上（`create_all` 不建索引，需显式补建） | Goal 5 |
| `_BASELINE_REVISION = "0001_baseline"` | 模块级 | legacy 分支 `stamp` 的目标 revision（**PG 的 `0001`，不是 MySQL 的**） | Goal 5 |
| `_FORWARD_COMPATIBLE_REVISION` | 模块级 | 只为 PG 滚动前向 | Goal 5 |
| `_validate_forward_schema()` | — | 依赖 `_CANONICAL_0019_SCHEMA_FLOOR` | Goal 5 |
| `_run_baseline_create_all_sync()` | — | legacy 分支专用 | Goal 5 |
| `_PG_LOCK_KEY` / `_postgres_lock()` | — | PG 专属 | Goal 5 |
| 两个反向 pin 测试 | `test_baseline_table_names_constant_matches_0001` / `..._index_names_...` | 只 pin **PG 链**的 `0001_baseline`；MySQL 侧**不改它们** | Goal 5 |
| `_run_create_all_sync()` | — | ⚠️ **不删** —— SQLite / 开发路径仍在用；只是 **MySQL 不用它** | 保留 |

> ⚠️ **一个必须避免的误操作**：不要为了"MySQL 用得上"而**改写**这些常量的内容。
> 它们必须与 PG 的 `0001_baseline` 保持**逐字一致**，否则两个 pin 测试失败、
> PG 侧的 bootstrap 也会失去保护。
> **正确做法是"一个字都不动"，让它们随 PG backend 一起退役。**

### 11.8 bootstrap 的并发保护：不做数据库锁

**决策：不为 MySQL bootstrap 实现任何数据库分布式锁。**

所有 MySQL 实例都是**空库 fresh cutover**，运维约定为
**"先由单一 Migrator 执行 migration，然后再启动多实例"**（§11.0 的四步发布流程）。
⇒ `_postgres_lock()` / `_PG_LOCK_KEY` **随 PostgreSQL backend 在 Goal 5 退役，不迁入 MySQL**。

**收益**：省掉一套锁的生命周期管理（连接持有、超时、异常释放、连接池交互），
也避免了"锁没释放导致启动卡死"这一类运维故障。

> ⚠️ **本节的"不做锁"只覆盖 bootstrap**；`run_events.seq` 与调度器预算锁
> **需要真正的锁**（§8.2 / §8.6），二者不可混为一谈。
>
> **如果未来真需要给 bootstrap 加锁**（例如允许 N 个实例同时 bootstrap）：
> MySQL 的等价物是 `GET_LOCK`，而它在业务场景被拒绝的理由
> （"**connection-scoped**，连接归还池时锁不释放"）**在本场景不成立** ——
>
> | | 业务事务锁（拒绝 `GET_LOCK` 的场景） | bootstrap 锁 |
> | --- | --- | --- |
> | 临界区边界 | 一个**短事务**；事务结束后连接**立刻归还池** | **整个 bootstrap 过程**；连接**被持有到结束** |
> | `GET_LOCK` 是否匹配 | ❌ 锁存活期与事务不匹配 ⇒ 锁泄漏 | ✅ 锁存活期 == 连接持有期 == 临界区 ⇒ **语义正好匹配** |
>
> 正确做法是 `SELECT GET_LOCK('<name>', <timeout>)` + `RELEASE_LOCK('<name>')`，
> 并**显式在同一连接上持有到 bootstrap 结束**。
> **但本阶段不需要它。**

### 11.9 MySQL migration 验收标准

**A. 迁移产物本身（运维侧执行）**

1. **空 MySQL 8.0.24 实例**（无任何表、无版本表）；
2. 从 **`0001_mysql_baseline`** 开始，**顺序执行** MySQL 后续 revision；
3. 最终 **只有一个 MySQL head**（在 MySQL 链上 `alembic heads` 返回单值）；
4. **Fresh bootstrap 不依赖任何 PostgreSQL migration，也不含任何 PostgreSQL-specific SQL**
   —— 无 `JSONB` / `BYTEA` / `pg_index` / `to_regclass` / `CREATE INDEX CONCURRENTLY` /
   advisory lock / partial index / `RETURNING`；
5. 执行完毕后的实际 schema **与 ORM 模型一致**（**12 张应用表 / 139 列**）；
6. **重复执行幂等**（`versioned` 状态下 `upgrade(head)` 无副作用）；
7. **与 PG 链互不影响**：MySQL 链的 `script_location` / `version_table` 均不读写 PG 链的任何状态。
8. 🔴 **Checkpoint 迁移产物与 Alembic 不冲突**：
   `checkpoint*` 4 张表不在 MySQL 链的任何 revision 中；
   `alembic_version` 与 `checkpoint_migrations` **互不干扰**；
   且 **Checkpoint 产物按 §11.4.2 的"线性 + 版本表守卫"执行**。

**B. 🔴 生产权限模型（Goal 4 的核心验收）**

9. **用"无 DDL 权限"的账号启动 Runtime，进程正常起来并正常工作**；
10. **未执行迁移时 → fail closed**（`readiness = false` + 明确报错文案，§11.5.2）；
11. **已执行迁移后 → `ready`**；
12. **Runtime 全程没有任何自动建表 / 改表**（可通过权限为空这一点直接反证；
    也可在 DB 侧观察 `information_schema` 的 DDL 计数）；
13. **目标库不存在时，Runtime 启动失败**（而不是自动创建，§11.3.1）。

---

## 12. 实施顺序

> 排序原则：**最高风险优先验证**。
> 核心原则：**先做 Checkpoint 持久化（Goal 2）** —— 如果这一层不可接受，
> 就没有必要继续大规模改 Application Data。

```
Goal 0  Runtime 范围清理（删除）          ── ✅ DONE（a55e5734）
Goal 1  MySQL 兼容性与风险 Gate（验证）    ── 下一步（G1-A / G1-B / G1-C）
Goal 2  MySQL 持久化底座（含 Checkpoint）  ── 最高风险，最先做（前置：V1 / V5）
Goal 3  应用 SQL 与并发迁移
Goal 4  全量 MySQL 集成验证（含权限模型）
Goal 5  删除 PostgreSQL
        ─────────────────────────────────
        Compliance Pass / Redis / 对象存储溢出  →  不在 Goal 内（§15.9）
```

> 🔴 **贯穿全部 Goal 的一条硬约束**（§11.0）：**生产 Runtime 不执行任何 DDL。**
> 每完成一个 Goal，都要重新确认这一点没有被新代码破坏
> —— 具体做法见 Goal 4 的权限模型验收（用无 DDL 权限账号启动）。

### Goal 0 ✅ DONE：Runtime 范围清理

> 🔴 **状态：已完成。产出提交 `a55e5734`**
> —— `refactor(runtime): remove channels, background MCP tasks, subagent batches, and LangGraph Store`
> （分支 `feat_portal`；工作区干净）。审计记录见 `docs/architecture/mysql-goal0-audit.md`。
>
> **⛔ 不要把下列任何一项再列为 G1 的"待实施前置项"** —— 它们已经落地。

| # | 能力 | 结果 | 复核方式 |
| --- | --- | --- | --- |
| 1 | Channel / GitHub Webhook | ✅ **已删除** | `app/channels/`、`gateway/github/` 消失；生产代码扫描零引用 |
| 2 | `mcp_tasks`（含持久化表与后台任务工具） | ✅ **已删除** | 生产代码扫描零引用 |
| 3 | `subagent_batches` / `subagent_batch_items` | ✅ **已删除** | 同上 |
| 4 | LangGraph Store（`BaseStore` 抽象） | ✅ **已删除** | `runtime/store/{provider,async_provider}.py` 消失 |
| 5 | memory thread metadata | ✅ **按最终实现保留** | `MemoryThreadMetaStore` 保留能力、改内部 dict，**不再依赖 LangGraph `BaseStore`**（§7.8 决策 6） |
| 6 | 普通 MCP（Tool / Server / OAuth） | ✅ **保留** | 只删了 `mcp_tasks` |
| 7 | 普通 SubAgent `task` | ✅ **保留** | 只删了 `subagent_batches` |
| 8 | 最终 ORM 表集合 | ✅ **稳定在 12 张应用表 / 139 列** | `Base.metadata` 反射实测（§2.2） |

> ⚠️ **两处容易误判的残留**（都不代表能力还在）：
> ① `persistence/{mcp_tasks,subagent_batches,channel_connections,webhook_delivery}/` 目录下
> **只剩未跟踪的 `__pycache__`**，源码已删、`models/__init__.py` 也不再 import 它们；
> ② `persistence/migrations/versions/0011_mcp_tasks.py` 与 `0016_subagent_batches.py`
> **仍被 git 跟踪** —— 它们属于**不可变的 PG 历史链**，只作审计，**不由 Gateway 回放**。

> **为什么它必须在最前面**：本 Goal 的产出是"**更小的表集合**"。
> MySQL 链的链根 **`0001_mysql_baseline`** 是从当时的 ORM 元数据生成的 ——
> 若这些模块尚未删除，baseline 会把即将消失的表一起建出来。
> ⇒ **删除必须先于 `0001_mysql_baseline` 的编写。**（现已满足）

> 📌 **删除的完整实施记录**（逐文件清单、接线点、测试清单、`DeltaChannel` 同名易误删清单等）
> 见过程留档 `mysql-migration-plan.md` §12 Goal 0。**它们已执行完毕，不是待办。**

### Goal 1：MySQL 兼容性与风险 Gate（**只做验证与设计，不改 ORM**）

> 🔴 **本 Goal 不包含任何大规模 ORM 改造**。产出是"**Gate 结论 + 迁移产物设计**"。
> ⛔ **不要让 G1 提前进入 G2 的全面实现** —— 本 Goal 的产出是**结论与设计**，不是运行代码。
>
> **前置 Gate 状态**：V3 ✅ / V6 ✅ / V2 ✅ 已关闭；**V1 🟡 待做**；**V5 🔴 待做（最高优先）**。

#### G1-A — V1 Alembic Chain Gate（MySQL 独立迁移链）

**确认以下六件事**（全部是"接线 + 校验"，不是"跑迁移"）：

| # | 要确认的事 | 依据 |
| --- | --- | --- |
| 1 | **MySQL 使用独立的 migration chain**（第二个 `script_location`） | §11.1 / §11.2 |
| 2 | **链根是 `0001_mysql_baseline`**，且它是从 **G0 之后的 ORM 元数据**生成的（12 表 / 139 列） | §11.3 |
| 3 | **PG legacy chain 与 MySQL chain 不串联** —— 不重放 PG `0001`–`0023`；MySQL chain 只有 `0001_mysql_baseline` 一条 revision | §11.1 |
| 4 | **Runtime 不跑 migration** —— 生产路径上不出现 `alembic upgrade` / `stamp` / `create_all` / `bootstrap_schema` 的执行语义 | §11.0 / §11.3.3 |
| 5 | **Application Schema migration 由运维 / DBA 执行**（独立高权限账号） | §11.0 / §11.3.2 |
| 6 | **schema revision verification** —— Runtime 只读检查 `alembic_version == 当前应用要求的 revision`，不匹配即 **fail closed / `readiness=false`** | §11.5 |

**具体实施要点**：

1. 冻结基线：记录 **PG 链** `alembic head = 0023_user_preferences`，确认 `0001`–`0023` **不可变**。
2. 按 **§11.2** 落地 **MySQL 独立链的接线方式** ——
   ① 第二个 `script_location`；
   ② 🔴 **从生产路径上直接删除 `_HEAD_REVISION` / `_KNOWN_REVISIONS` 这两个模块级缓存**；
   ③ `bootstrap_schema(..., backend="mysql")` 分支（`empty` / `versioned` / `legacy → refuse` 三态，
   **无** forward-compatible 分支）。
   ⛔ **不要构建通用 multi-chain framework**：不做链注册表、不做 per-chain 类层次、
   不做 cache manager。**只做"backend 选择对应 script location"这一件事。**
3. **验收标准**：§11.9 的 **A 组 8 条**（迁移产物）+ **B 组 5 条**（无 DDL 权限账号可正常启动）。

#### G1-B — V5 CheckpointSaver Gate（**对已冻结依赖做兼容性 / 正确性验证**）

> 🔴 **选型已冻结**（§0）：实现基线是 `langgraph-checkpoint-mysql[asyncmy]==3.0.0`。
> **V5 不重新讨论"是否使用这个包"**，也不做第三方 Saver 市场选型。
> 它的唯一职责是：**验证这个已确定的依赖在当前 DeerFlow Runtime 中是否满足所需的运行语义和正确性。**
> ⚠️ **仍然保留真实验证失败的可能性** —— 不要把 V5 描述成"肯定通过"。

1. **依赖与驱动确认**（§7.7）：
   - `asyncmy` 作为**异步 Application / Checkpointer 主路径**；
   - `PyMySQL` 作为**同步路径**，唯一用途是 `SqlAgentStore` —— 🔴 **必须保留**；
   - ⛔ **不启用同步 MySQL CheckpointSaver**。
   - **验收**：`grep -rn "app_sync_sqlalchemy_url"` 的每个调用点都有明确结论。
2. **在真实 MySQL 8.0.24 上**按 §7.3.1 的清单逐项做实：
   `aget_tuple` / `alist` / `aput` / `aput_writes` / `adelete_thread`、
   pending writes / resume / interrupt / retry / rollback / **branch / regenerate**、
   concurrent checkpoint writes / long conversation / `checkpoint_channel_mode=full`、
   **asyncmy pool integration** / **event-loop lifecycle**、
   **大 checkpoint payload** / **`INSERT IGNORE` 风险** / **`max_allowed_packet`**。
3. ⚠️ 同时核对 §7.2 末尾的**三处**实现细节（`INSERT IGNORE` 的取值长度、大 blob 的吞吐/内存与
   base64 膨胀、`asyncio.get_running_loop()` 的构造约束）。
4. **接入设计**：把 Checkpointer 的构造从 `provider.py` / `async_provider.py` 的具体类
   抽象为"按 `config.type` 分派的工厂"，新增 `mysql` 分支。
   —— 只需扩展 `CheckpointerType` 的 `Literal`，不需要新配置模型。
   ✅ **保留同步 Checkpointer 工厂分支**：不是为生产，而是为了让
   `DeerFlowClient` / 测试**继续可用** —— 同步分支只需支持 `memory` / `sqlite`，**不加 `mysql`**。
5. 🔴 **验收结论必须落在 §7.3.2 处置阶梯的某一档**（第 1️⃣–5️⃣ 档），
   并明确写出：**是接入问题 / 需要包装 / 需要子类化 / 需要 vendor+patch / 是 architecture blocker**。
6. 🔴 **⛔ 不要在 V5 通过之前把第三方源码复制进正式 Runtime 代码** ——
   默认路径是**精确版本依赖**；只有当 §7.3.2 第 4️⃣ 档被真实触发时，才 vendor **同一个 3.0.0**
   （届时才需要 V7 的 `LICENSE` / `UPSTREAM.md`）。
7. **必须完成于 Goal 2 的代码之前。**

> 📌 **已关闭 Gate 的登记**（不构成 G1 工作项，仅供引用）：
> **V2** —— `checkpoint_ns` 主键参与已换成固定 16 字节 `checkpoint_ns_hash`（§11.4.3）。
> **V6** —— MySQL `JSON_TYPE()` 返回**大写**，字面量**必须大写**（§6.5）。
> **V3** —— `run_events.seq` 串行化方案已定（锁 `threads_meta` 行，§8.6）。
> **V7** —— **仅在 vendor 路径生效**，默认路径下工作量归零（§13）。

#### G1-C — Production Migration Model（生产迁移执行模型）

**最终确认**下面这条完整链路，并把它写成可交付的设计（§11.0）：

```
① Application migration artifact      → 运维 / DBA 用高权限账号执行（alembic upgrade head）
② Checkpoint migration artifact       → 运维 / DBA 用高权限账号执行（database/mysql/checkpoint/）
③ Runtime zero DDL                    → 启动路径上不出现任何 CREATE / ALTER / DROP / stamp / upgrade
④ schema / version verification       → Runtime 只读校验（Application revision + Checkpoint 版本）
⑤ fail closed                         → 校验不通过 ⇒ 启动失败 / readiness=false，禁止自动修复
```

**要交付的设计内容**：

1. **Application 迁移产物**（§11.3）：MySQL 独立链的 `script_location` / `version_table` / 精简 `env.py`；
   **不移植自动建库** —— 库不存在就启动失败（§11.3.1）。
2. **Checkpoint 迁移产物**（§11.4）：`database/mysql/checkpoint/` 的**线性 + 版本表守卫** SQL 产物
   （由上游 `MIGRATIONS` 的 22 条**程序化渲染**，**不手写**）；
   🔴 **产物必须与 `langgraph-checkpoint-mysql==3.0.0` 版本绑定**（§11.4.4）。
3. **Runtime 只读校验设计**（§11.5）：6 项校验 + fail closed + 明确报错文案。
   ⛔ **禁止调用 `setup()` 自动修复。**
4. **明确分工**：谁执行 DDL（运维 / DBA）、谁只读校验（Runtime）、谁定义 revision（本方案）。
5. **Dev / Test 与生产的隔离**：靠**调用路径**，不靠 `auto_setup` 开关（§11.6）。

**G1 的横切约束**（适用于以上三项）：

- 🔴 **直接实现 MySQL 最终路径，不新增并发 abstraction。**
  `acquire_txn_lock(key)` / `allocate_sequence(table, col, where)` / `conditional_upsert(...)`
  这类抽象层**不建**，理由：调用点很少（2 处 advisory lock + 4 处 `RETURNING` + 1 处 `json_match`）；
  PostgreSQL 最终会被删除，该 abstraction 只在短暂的 PG/MySQL 共存期有价值；
  抽象层会**掩盖方言差异**，反而增加"以为测过、实际没测"的风险。
  ⇒ 直接在调用点实现 MySQL 分支（`if dialect == "mysql": …`）。
- **CI 静态检查**：用 MySQL 方言编译全部 ORM 语句，断言不出现 `RETURNING`（§4.9-A 的静默失效面）。

### Goal 2：MySQL 持久化底座（**最高风险，最先做**）

> 🔴 **本 Goal 的核心约束**：**生产 Runtime 不执行任何 DDL。**
> 因此"接入 CheckpointSaver"这件事被拆成**三件不同的事、三个不同执行方**（§11.0）。

**2-A. 依赖与驱动**

1. 在 `harness/pyproject.toml` 的 `[project.optional-dependencies]` 新增 `mysql` extra：
   ```toml
   mysql = [
       "asyncmy>=0.2.10",                                  # 异步 Application / Checkpointer 路径
       "PyMySQL>=1.1.1",                                   # 同步 SqlAgentStore 路径（必须保留，§7.7）
       "langgraph-checkpoint-mysql[asyncmy]==3.0.0",       # 🔴 已冻结；精确固定（§0/§7.4）
   ]
   ```
   - ⛔ **不加 `orjson`**（上游声明但包内零命中）。
   - ⛔ **不 vendor 源码**（除非 §7.3 第 4️⃣ 档被触发）。
   - ⚠️ **版本必须 `==` 精确固定** —— §11.5 的校验项 6 从包内 `MIGRATIONS`
     条目数推导"要求的迁移版本"，**上游变更条目数会直接改变校验基线**。
     ⛔ **不要用 `>=x,<x+1`**。
   - ⛔ **不启用同步 MySQL CheckpointSaver**。
2. 确认 `asyncmy` 与 `PyMySQL` 均为纯驱动依赖，不引入其他传递依赖。

**2-B. Checkpoint Schema 迁移产物（运维 / DBA 执行，§11.4）**

3. 🔴 生成 `database/mysql/checkpoint/`：
   - **程序化渲染**上游 `MIGRATIONS` 的 **22 条**（0-indexed 0–21）为线性 `.sql` 文件；
     ⛔ **不手写"等价" DDL**（§11.4.1 已列出 8 处手写必踩的坑）。
   - 产物形态：**线性 + 有序 + 只跑一次**；**幂等性由 `checkpoint_migrations` 版本表提供**。
   - 随附 `README.md`（来源 / 版本 / 顺序 / 版本表语义）与 `verify.sql`（只读校验）。
   - 记录 `SAVER_VERSION`（`3.0.0`）+ `MIGRATIONS_COUNT`（**22**）。
4. 在真实 MySQL 8.0.24 上**完整走一遍**：空库 → 22 条 → 4 张表 → 版本表 `MAX(v) = 21`。

**2-C. Application Schema 迁移产物（运维 / DBA 执行，§11.3）**

5. 🔴 创建 **`0001_mysql_baseline.py`（MySQL 链根，且本 Goal 唯一的新 revision）**：
   重建全部 **12 张表**（**139 列**、`DATETIME(6)`、3 个生成列唯一索引、
   1 个 FK、`DEFAULT` 补齐、31 个索引）。
   - 该 revision 自带完整建表 ⇒ **MySQL fresh bootstrap 不需要 `create_all`**。
   - 🔴 **它必须一次表达"最终形态"** —— 库是空的，
     **所有 `NOT NULL` / `server_default` / 唯一索引 / 生成列都直接写在建表语句里**，
     不经过"先放宽、后收紧"的两步走。
   - ⛔ 不含任何已删除对象（8 张表）。
   - ⛔ 不含 `checkpoint*` 4 张表（由 2-B 的产物创建）。
   - 🟡 若做 Compliance Pass：本 revision 同时写入 `ag_` 前缀与中文 COMMENT。
6. 修改 ORM 模型（`DateTime(timezone=True)` → `DATETIME(6)` + UTC 写入边界、
   `server_default` **按 §6.7 的审计结论**（**只保留 4 个确有 DB 层语义的字段，不机械新增**）、
   JSON 列**不动**、`ForeignKey` **不动**）。
7. 🔴 **给 `JsonMatch` 补 MySQL 方言分支**（§6.5，约 15 行）。
   ⛔ **不删除 `persistence/json_compat.py`**，也**不做 JSON → TEXT 改造**。
8. 按 §9.2 补 **2 个**能力补偿索引（occurrence claim 复合索引 + OAuth 全量唯一索引）。
   🟡 若做 Compliance Pass：按 §9.1 完成 31 个索引的等价改名。
9. ⛔ **不做**：应用层级联删除、孤儿巡检、JSON 拆 scalar 列（§6.5/§6.6）。

**2-D. Runtime 接入（只读，不建表）**

10. 🔴 在 `async_provider.py` 新增 `mysql` 分支（约 20 行，形态见 §7.4 改动点 1）：
    - 🔴 **删掉 `await saver.setup()`**（生产路径不含 DDL）。
    - `database_config.py:143` / `checkpointer_config.py:9` 追加 `"mysql"`。
    - **为什么 `conn=pool` 可行**：`_ainternal.get_connection` 先试 `hasattr(conn,"cursor")`，
      再试 `hasattr(conn,"acquire")`；`asyncmy.Pool` 有 `acquire()` ⇒ 走池分支。
11. 🔴 **新增 Runtime 的只读 Schema / 版本校验**（§11.5）：
    - 6 项校验：Application revision（恰好一行）+ 4 张 checkpoint 表存在 +
      `MAX(v) == len(MIGRATIONS) - 1`；
    - **fail closed** + 明确报错文案（§11.5.2）；
    - ⛔ **不自动修复、不自动升级、不自动建表**。
    - **落点**：`persistence/engine.py` 的启动路径改为**只校验不迁移**
      —— 现有 `init_engine()` 的 `CreateSchema` / `bootstrap_schema` / `_auto_create_postgres_db`
      三段 DDL **全部移出 Runtime**（§11.0）。
12. 🔴 **删除 Runtime 的自动建库逻辑**（§11.0 硬约束）：
    - ⛔ **不要把 `_auto_create_postgres_db` 改写成 MySQL 的 `CREATE DATABASE IF NOT EXISTS`**；
    - 目标库不存在时，Runtime **必须在启动期失败**。
13. ✅ **不引入** blob 混合策略、阈值配置、对象存储溢出、孤儿 GC（§7.6）。
    确认 `LONGBLOB` 单库直存可用。
14. 确认 `checkpoint_channel_mode = full` ⇒ **不挂载 `CachedHistorySaver`**
    （`async_provider.py:242-253` 的条件不成立）。

**2-E. 验收**

15. 用现有测试回归：
    `tests/test_delta_channel_checkpointers.py`、`tests/test_run_worker_delta_resume.py`、
    `tests/test_run_worker_rollback.py`、`tests/test_run_duration_checkpoint.py`、
    `tests/test_thread_regenerate_prepare.py`、`tests/test_threads_router.py`。
    ⚠️ **保持 `checkpoint_channel_mode = full`**。
16. ⚠️ **大 payload 实测**：构造长会话（大量 messages），测量单 blob 长度、
    `max_allowed_packet` 余量、写入/读取吞吐。**这是 §7.6 决策的实证依据。**
17. 🔴 **本 Goal 的退出条件（§11.9 的 A 组 + B 组）**：
    - A 组（迁移产物）：空实例 → `0001_mysql_baseline` → 顺序 upgrade → **单 head** →
      不依赖任何 PG migration / PG-specific SQL → 幂等 → 与 PG 链互不影响；
    - B 组（生产模型）：**用"无 DDL 权限"账号启动 Runtime 成功**；
      **未执行迁移 → fail closed**；**已执行迁移 → `ready`**；**Runtime 全程无任何自动建表 / 改表**。
18. **决策点（只剩"验证结果"这一维）**：
    - 若 V5 通过 → 继续，档位升为 `Moderate`；
    - 若 V5 失败 → **按 §7.3 的固定阶梯逐档处置**。
      ⛔ **不重新开启 CheckpointSaver 选型讨论**；只有走到第 ⑤ 档才回落档位（§1.4）。

### Goal 3：应用 SQL 与并发迁移

1. 🔴 **【Gate V3，✅ 已关闭，按已实测的修法落地】** V3 结论为**负面**：
   在 MySQL 8.0.24 + READ COMMITTED 下，`SELECT max(seq) … FOR UPDATE`
   **不提供串行化**（两个会话读到同一 MAX，后者 `1062`）；
   单语句 `INSERT … SELECT IFNULL(MAX(seq),0)+1` **同样失败**。
   ⇒ **采用已实测有效的修法**：先对 **`threads_meta` 行**取 `SELECT … FOR UPDATE` 行锁
   （锚点行不存在时先做幂等 upsert），再读 `max(seq)`（§8.6）。
   ⛔ **不要**采用"保留原语句 + 依赖 `uq_events_thread_seq`"的方案
   —— 当前**没有 `IntegrityError` 重试**，一次竞态 = 整个 batch 事务失败（**事件丢失**）。
2. 替换 **2 处**事务级 advisory lock（§8.2）：
   - `runtime/events/store/db.py:148`（按 V3 的行锁修法）；
   - `scheduled_task_runs/sql.py:317` 调度器全局预算锁 —— **锁表 sentinel row + `SELECT … FOR UPDATE`**，
     或 unique constraint + retry；**不使用 `GET_LOCK`**。
3. bootstrap 的会话级 advisory lock：✅ **按 §11.8 的结论落地** ——
   只允许单一 Migrator 执行 Alembic，然后启动多个应用实例，**不实现 DB 锁**。
4. 替换 **4 处** `RETURNING`（§8.3 的逐条设计）。
   ⚠️ **必须逐个改** —— SQLAlchemy 2.0.49 会把 `UPDATE … RETURNING` **静默编译成 MySQL 非法 SQL**
   （实测：编译期不报错，服务端 `1064`），**编译通过 ≠ 能执行**（§4.9-A）。
5. 显式设置 `READ COMMITTED`；审计全部 **28 处** `with_for_update` 的索引可用性（§8.7）。
   ⚠️ 计数口径：**按"包含 `with_for_update` 的行数"**（含方法链与
   `session.get(Row, id, with_for_update=True)` 两种写法）。
6. 重写 `app/gateway/auth/repositories/sqlite.py` 的错误码判定（§4.9-D）：
   - MySQL 1062 消息实测为 `Duplicate entry 'github-oid-9' for key 'users.idx_users_oauth_identity'`；
   - 现有三段回退在 MySQL 下**全部返回 `False`** ⇒ 必须改写。
7. **重新验证多实例并发正确性**：为 **Scheduler / Run ownership 两个职责域**
   分别写 MySQL 并发测试，
   并**首次定义 MySQL 上的跨实例对账语义**
   （`scheduled_task_runs/sql.py:758` 原文 "Multi-instance reconciliation is Postgres-only."）。
8. 🔴 **本 Goal 退出条件**：
   - §4.9-A 的四类**静默失效**（`RETURNING` / `date_trunc` / `EXTRACT(epoch)` / 亚秒截断）
     全部构造清除，并有 CI 静态检查兜底；
   - **两个**职责域的 MySQL 并发测试通过；
   - `run_events` 在高并发下**不出现 1062 导致的事件丢失**。

### Goal 4：基础设施与全量 MySQL 集成验证（**含生产权限模型**）

1. 依赖已在 Goal 2 落地（`mysql` extra）；本 Goal 只做**基础设施接线与集成验证**。
2. 🔴 **`persistence/engine.py` 的 DDL 段必须整体拆掉**（§11.0）：
   - 新增 MySQL engine kwargs（`pool_pre_ping`、`pool_recycle`、`max_execution_time`）；
   - ⛔ **`_auto_create_postgres_db` 直接删除** —— **不要**改写成 MySQL 的 `CREATE DATABASE IF NOT EXISTS`；
   - ⛔ 移除 `_ensure_postgres_schema()` 的 `CreateSchema`；
   - ⛔ 移除 `bootstrap_schema(...)` 的调用；
   - ✅ 改为**只读校验**（§11.5）；目标库不存在时**启动期直接失败**。
3. `app/gateway/health.py`：新增 `mysql` 探针（`_probe_checkpointer_backend` 的
   `Literal` 也要扩展），并把 §11.5 的校验结果接到 readiness。⛔ **不需要 Store 探针**（§7.8）。
   ⚠️ `health.py:176` 引用 `runtime/store/_sqlite_utils.py` ⇒ **删 Store 时该文件必须保留**。
4. 🔴 **6 处能力守卫必须逐处扩展 `mysql`**（否则进程直接拒绝启动）。
   ⚠️ **注意：它们不是同一种写法** —— 前两处是 `!= "postgres"`，其余是 `not in ("sqlite","postgres")`：

   | 位置 | 判定写法 | 触发条件 | 失败语义 |
   | --- | --- | --- | --- |
   | `app/gateway/deps.py:79` | `backend != "postgres"` | `scheduler.multi_instance=true` | **`SystemExit`** |
   | `app/gateway/deps.py:92` | `backend != "postgres"` | `GATEWAY_WORKERS > 1` | **`SystemExit`** |
   | `app/gateway/deps.py:128` | `not in ("sqlite","postgres")` | `agent_storage.backend: db` | **`SystemExit`** |
   | `persistence/agents/__init__.py:47` | `not in ("sqlite","postgres")` | `agent_storage` 构造 | `ValueError` |
   | `persistence/managed_subagents/__init__.py:32` | `not in ("sqlite","postgres")` | 同上 | `ValueError` |
   | `app/gateway/health.py:229` | `not in ("sqlite","postgres")` | checkpointer 探针 | ⚠️ **返回 `DATABASE_UNREACHABLE`**（不是抛异常） |

   > 🔴 **只加驱动不改守卫 ⇒ 进程起不来。**

   ⚠️ **同时扩展两个驱动/方言 `Literal`**（`database_config.py:143`、`checkpointer_config.py:9`）——
   那类只是校验失败，不会拒启动，**两者要分开列**。
5. **部署（按真实环境收缩，§3.4）**：
   - ✅ 在 `docker/*.yaml` 增加 MySQL 8.0.24 服务，并用 `MYSQL_DATABASE` 建库；
   - ✅ 增加一个**一次性迁移 job**（先 Application Schema，再 Checkpoint Schema）；
   - ✅ 支持 external MySQL DSN；
   - ✅ health / readiness 覆盖；
   - ✅ 多实例闸门覆盖；
   - ⛔ **不新增 Kubernetes / Helm 的 MySQL 部署形态**。
6. **测试策略（不做双数据库参数化矩阵）**：

   | 类别 | 处置 |
   | --- | --- |
   | backend-neutral 行为测试 | ✅ **继续保留**（它们本来就与后端无关） |
   | PostgreSQL 专属测试（**7 个**） | 🔻 **由对应的 MySQL 测试就地替换**，不保留 PG 版本 |
   | 命中 `postgres` 的 **91 个**文件 | 🔻 **不做 `pytest × PG × MySQL` 参数化** —— PG 最终会删除。只把**断言 PG 专有行为**的部分改为 MySQL 断言 |
   | MySQL correctness 测试 | ✅ **重点覆盖真实风险**：`RETURNING` 改写、advisory lock 替换、partial unique 生成列、`run_events.seq` 并发、`JsonMatch` 大小写、时间精度 |
   | ORM / framework 自身行为 | ⛔ **不重复验证**（Risk-Adjusted Verification） |

7. 🔴 **全量集成验证（在真实 MySQL 8.0.24 上）**：

   | # | 验证项 | 判据 |
   | --- | --- | --- |
   | 1 | Gateway 启动 | 正常 |
   | 2 | agent 对话 | 正常 |
   | 3 | checkpoint resume | 正常 |
   | 4 | interrupt / retry / rollback | 正常 |
   | 5 | Scheduler | 正常 |
   | 6 | 多实例 | 正常 |
   | 7 | 长会话（大 payload） | 正常 |
   | 8 | 🔴 **生产化权限模型** | **应用 Runtime 用"无 DDL 权限"账号（只有 `SELECT`/`INSERT`/`UPDATE`/`DELETE`）启动并运行正常** |
   | 9 | 🔴 **未执行迁移** | **fail closed**，readiness = false，报错文案明确 |
   | 10 | 🔴 **已执行迁移** | readiness = true |
   | 11 | 🔴 **Runtime 无自动建表 / 改表** | 用 `information_schema` 或审计日志确认全程零 DDL |

   > **第 8–11 项直接验收 §11.0 的硬约束。**
   > **用权限本身作为验收手段**：如果 Runtime 仍残留任何 DDL，它会**因为权限不足而失败**，
   > 这是一个**无法被配置掩盖**的判据。
8. **分阶段切流**：利用 `database:` 与 `checkpointer:` 可分别配置的能力，
   **先切一个职责域、再切另一个，不要一次性切换**。
   ⚠️ 每个职责域切走后**单向不回退**（§2.1：不设计数据层回滚）。
9. ⚠️ **切流期必须保持 `checkpoint_channel_mode = full`**（§2.5）。

### Goal 5：稳定期结束后彻底删除 PostgreSQL

1. 删除 PG Driver、PG Saver、PG Schema helper、PG 专属 SQL
   与 `persistence/json_compat.py` 的 **PG 方言分支**（保留 sqlite / mysql 分支）。
2. 🔴 **同时删除 §11.7 的 legacy 清单**：
   `_CANONICAL_0019_SCHEMA_FLOOR`、`_BASELINE_TABLE_NAMES`、`_BASELINE_INDEX_NAMES`、
   `_BASELINE_REVISION`、`_FORWARD_COMPATIBLE_REVISION`、`_validate_forward_schema()`、
   `_run_baseline_create_all_sync()`、`_postgres_lock()` / `_PG_LOCK_KEY`，
   以及 `bootstrap.py` 的 `legacy` 与 `forward-compatible` 两个分支
   与两个反向 pin 测试；**并移除 PG 链的 migrations 目录**（含 `0001`–`0023`）。
   —— 注意：`_run_create_all_sync()` **保留**（SQLite / 开发路径仍在用）。
   —— 注意：`_HEAD_REVISION` / `_KNOWN_REVISIONS` **已在 Goal 1 从生产路径删除**
   ⇒ 本 Goal 只需确认没有残留引用。
3. 🔴 **确认 `runtime/store/*` 无残留引用**（Store 不迁移，§7.8）：
   若 Goal 0 已完成，则本步只需确认。
   ⚠️ **必须保留 `_sqlite_utils.py`**（其落点已在 Goal 0 决定）。
4. 删除 Helm PostgreSQL 配置、compose 的 PG 服务、`postgres` extra 与依赖。
5. 删除 / 改写 PG 专属测试（含 `_PG_LOCK_KEY` 相关用例）。
6. 文档：更新 `backend/AGENTS.md`、`config.example.yaml`、`README*`，
   并明确"最终只维护 MySQL"这一结论。
7. ✅ **PG 删除后只剩一条 MySQL 链** ⇒ 双链期结束，**不留任何"以防万一"的双分支**。

---

> 🔻 **Redis 侧的可靠性 / 性能优化整体移出本方案**（§2.5 / §15.9）。
> Redis 的 checkpoint cache / delta 缓存 / 鉴权读缓存 / StreamBridge replay /
> sandbox ownership 持久化，**全部作为独立变更单独设计与交付**，
> 本迁移方案**不为它们做任何预置设计**。
> 唯一保留的一条约束：**Redis 只承载"可从 durable store 重建"的状态**，
> 属 lease / correctness 的状态**不得放入"重启即空"的 volatile Redis**。

---

## 13. 实施前 Gate

> 🔴 **CheckpointSaver 选型已冻结**（§0）⇒ **V5 不是 selection Gate**。
> V5 的职责是**验证已确定的 `langgraph-checkpoint-mysql[asyncmy]==3.0.0`
> 在当前 DeerFlow Runtime 中是否满足所需的运行语义和正确性**（§7.3.1）。
> ⚠️ **它仍然可能失败** —— 失败时按 §7.3.2 的固定阶梯处理，**不重新选型**。

| Gate | 对应 G1 子项 | 内容 | 当前状态 | 必须完成于 | 不做的后果 |
| --- | --- | --- | --- | --- | --- |
| **V1** | **G1-A** | **MySQL 独立链的接线方式** —— ① 第二个 `script_location`；② **不新增缓存抽象**，直接删掉 `_HEAD_REVISION` / `_KNOWN_REVISIONS` 的生产用法（§11.2）；③ 运维执行的 `alembic upgrade head` + Runtime 只读校验（§11.3 / §11.5）。**验收：§11.9 的 A 组 8 条 + B 组 5 条** | 🟡 **待做** | 🔴 **Schema 实施之前（Goal 2）** | 链配置写错会导致 **head 判定串链**、`upgrade` 跑错链 |
| **V5** | **G1-B** | 🔴 **对已冻结依赖 `langgraph-checkpoint-mysql==3.0.0` 做兼容性 / 正确性验收**（§7.3.1）—— **在真实 MySQL 8.0.24 上**跑通 `aget_tuple` / `alist` / `aput` / `aput_writes` / `adelete_thread`，用现有回归测试验证 pending writes / resume / interrupt / retry / rollback / **branch / regenerate** / 长会话 / 并发写 / `checkpoint_channel_mode=full` / **asyncmy 池接入** / **event-loop lifecycle**；并核对大 checkpoint payload、**`max_allowed_packet`**、`INSERT IGNORE` 风险、base64 线路膨胀。**验收结论必须落在 §7.3.2 阶梯的某一档。** ⛔ **不要在 V5 之前把第三方源码复制进正式 Runtime 代码** | 🔴 **待做（最高优先）** | 🔴 **Goal 2 的代码之前** | 误判"可用"→ 上线后才发现 checkpoint 语义缺口；误判"不可用"→ 白白走 vendor |
| **V1+V5+模型** | **G1-C** | **生产迁移执行模型**（§11.0）：Application 迁移产物 + Checkpoint 迁移产物 + **Runtime zero DDL** + schema/version 校验 + **fail closed**；Checkpoint 产物与 Saver 版本绑定（§11.4.4） | 🟡 **待做（设计）** | 🔴 **Goal 2 的代码之前** | 权限模型被新代码破坏 ⇒ 生产启动即失败或隐式建表 |
| **V3** | — | `run_events.seq` 在 MySQL 8.0.24 + READ COMMITTED 下的并发分配语义 | ✅ **已关闭（结论为负面，修法已定）**：`SELECT MAX(seq) … FOR UPDATE` **不提供串行化**；单语句 `INSERT … SELECT` 同样失败；**修法已实测有效** —— 先对 `threads_meta` 行取行锁（锚点缺失时先幂等建行，§8.6） | ✅ 已完成 | 已识别 ⇒ 直接进入 Goal 3 实施 |
| **V6** | — | `JsonMatch` 的 MySQL 分支 —— MySQL `JSON_TYPE()` 返回**大写** | ✅ **已实测确认**：`INTEGER` / `STRING` / `BOOLEAN` / `NULL` / `DOUBLE` / `OBJECT` / `ARRAY`。字面量必须大写，否则谓词**恒 false（静默错误）** | Goal 3 的 `json_compat.py` 改动之后 | 线程置顶 / 归档过滤静默失效 |
| **V2** | — | `checkpoint_ns` 的真实最大长度 | ✅ **已关闭，不再阻塞**：上游最终 schema 把主键参与换成固定 16 字节的 `checkpoint_ns_hash` ⇒ **索引键长与 ns 长度无关**；仅剩 `varchar(2000)` 列宽限制，而 DeerFlow 顶层**始终传 `""`**（§11.4.3） | ✅ 已完成 | 无（已从设计上消除） |
| **V7** | — | **vendor 合规与可维护性** | 🔻 **默认路径下不适用** —— 默认是**精确版本直接依赖**，**无 vendor、无 LICENSE 副本、无 `UPSTREAM.md`**。**仅当 §7.3 第 4️⃣ 档（vendor 同一个 3.0.0 + 最小 patch）被触发时**本 Gate 才生效：① `LICENSE` 全文保留（MIT / `Copyright (c) 2024 Theodore Ni`）；② `UPSTREAM.md` 记录版本 / 发布日 / commit SHA / 逐文件改动状态 / 升级步骤；③ `mysql` extra 相应调整 | 仅在 vendor 路径生效 | 许可合规风险；后续无法重放本地改动 |

**已关闭 Gate 的净效果**：

| Gate | 关闭后消除了什么 |
| --- | --- |
| **V3** | 从"待实测风险"变成"**方案已定的实施项**" —— 不再是档位退化条件（§1.4） |
| **V6** | 从"待确认"变成"**已确认的事实 + 一条明确的编码要求（大写）**" |
| **V2** | 从"阻塞项"变成"**结构上已不存在的问题**"，无需实测、无需断言兜底 |
| **V7** | 从"必做"变成"**条件适用**" —— 默认路径下工作量归零 |

> ⚠️ **V4（Redis StreamBridge 的 SSE 恢复路径）不属于本次变更范围**（§2.5 已把 Redis 整体移出）。
> 已知事实：gap 检测**只覆盖保留窗口被裁掉**（`redis.py:291` 要求 `earliest_entries` 非空），
> **不覆盖 Redis 重启丢流**；SSE 路径**没有** RunEventStore 自动补偿，
> 恢复依赖客户端 `reload_durable_state`。
> **不要声称"`run_events` 会自动回源"。** 该问题与 MySQL 迁移**无因果关系**。

---

## 14. 风险清单

### 14.1 三大风险

#### 风险 1：Checkpoint 持久化的运行语义不达标（**最高**）

- **事实**：官方无 MySQL saver（实测只有 base/memory/postgres/serde/sqlite）；
  **`langgraph-checkpoint-mysql` 3.0.0 已被冻结为本次迁移的实现基线**（§0），
  §7.2 的 22 项静态验收覆盖了 DeerFlow 全部真实使用的 5 个异步方法，
  且**默认以精确版本依赖引入**（§7.4）。
- **为什么仍然危险**：
  - 上游是**非官方组件**（MIT / Theodore Ni），需自行承担升级与缺陷跟踪责任；
  - **三处**实现细节需复核：`INSERT IGNORE` 的错误吞噬、`json_arrayagg` 的 base64 线路膨胀、
    `asyncio.get_running_loop()` 的构造约束；
  - ⚠️ **V5 尚未执行** —— 在实测通过之前，档位仍按 `significant changes` 计。
    **选型冻结不等于语义已被证实。**
- **缓解**：
  1. **V5 是最高优先 Gate**，必须在 Goal 2 的代码之前完成（§12 Goal 1 / G1-B）；
     **V7 仅在走 vendor 路径时适用**；
  2. **保留回退路径**（§7.3 / §7.5）：先排查**接入方式** → adapter / wrapper →
     子类化 `DeerFlowMySQLSaver(AsyncMySaver)`；确需改上游内部实现时才
     **vendor 同一个 3.0.0 + 最小 patch**（改动登记进 `UPSTREAM.md`）
     —— 不需要"从零设计表结构与全部 SQL"，也**不需要预先 fork**；
  3. **大 payload 实测**（Goal 2 的 2-E 节）为 §7.6 的决策提供实证，
     并覆盖 **`max_allowed_packet`** 上限；
  4. ⚠️ **本风险不能靠 Redis 缓存缓解**：`delta + Redis checkpoint cache`
     **不与数据库迁移同时切换**（§2.5），因此迁移期**必须按"无缓存"评估性能**。

#### 风险 2：并发语义变化（**高**）

- **事实**：**2 处事务级 advisory lock** + 1 组会话级 advisory lock、
  **4 处 `RETURNING`**、**3 处 partial index（其中 2 处需生成列）**、
  **28 处 `FOR UPDATE`**、默认隔离级别从 RC 变 RR。
- **为什么危险**：这些是"能跑但可能静默错误"的一类问题。最典型的场景：
  - `scheduled_task_runs` 的 `occurrence_seq` 分配改用 `LAST_INSERT_ID(expr)` 后
    与 `run_events` 的 `AUTO_INCREMENT` 语义互相污染（**序号串号**）；
  - RR 下的 gap lock 让 claim 语句的"跳过"行为与 PG 不同，导致偶发死锁（`1213`）；
  - **`UPDATE … RETURNING` 在 SQLAlchemy 下编译期不报错**，
    意味着现有测试（包括断言编译 SQL 的测试）**不会**捕获这个错误；
  - **`run_events.seq` 在 MySQL + RC 下失去串行化**（§8.6）。
- **缓解**：为每个并发原语写 **MySQL 专属的并发回归测试**；
  显式 `SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED`；
  用 `scripts/benchmark/concurrency/worker.py` 做双 Worker 压测；
  对 **4 处** `RETURNING` 加**静态检查**。

#### 风险 3：Schema 层的硬约束（**中**）

- **事实**：
  1. InnoDB 索引键长上限 3072 字节 —— 12 张表 + checkpoint 表**无越界键**（§6.9）。
  2. `DATETIME` 默认精度 0 → **截断亚秒**，破坏 `scheduled_task_runs` 的 FIFO
     排序与 `expire_queued_runs` 的过期判定（**29 个时间列**受影响）。
  3. `DateTime(timezone=True)` 在 MySQL 上**静默丢弃时区**。
  4. MySQL `JSON_TYPE()` 返回**大写**，`JsonMatch` 的方言分支写错会**静默失效**（§6.5）。
  5. ⚠️ **部署侧**：`max_allowed_packet` 必须能容纳单个 checkpoint blob（§7.6）。
- **为什么危险**：这类问题在开发环境（小数据、单进程、UTC 单时区）**不会暴露**，
  只会在生产（长会话、多 Worker、跨时区）暴露。
- **缓解**：所有时间列显式 `DATETIME(6)` + 应用层统一 UTC 写入边界；
  `JsonMatch` 分支用现有置顶/归档用例回归（V6）；
  `max_allowed_packet` 进部署 checklist。
  ⚠️ **组织侧缓解**：**`0001_mysql_baseline` 必须一次写全裁剪后的 Schema**，
  因此它的 review 是本风险最集中的一道闸门。

### 14.2 风险清单（按严重度）

| # | 风险 | 严重度 | 可能性 | 缓解 |
| --- | --- | --- | --- | --- |
| 1 | **已冻结的第三方 Saver**（`langgraph-checkpoint-mysql==3.0.0`）的运行语义/性能不达标（**V5 未做**） | 高 | 中 | **V5 验收**；失败时按 §7.3 的**固定阶梯**：接入方式 → adapter/wrapper → **子类化** → 确需改上游内部实现才 **vendor 同一个 3.0.0 + patch**。⛔ **不重新选型** |
| 2 | `UPDATE … RETURNING` 编译期不报错 → 运行期才失败 | 高 | **高** | CI 静态检查（MySQL 方言编译 + 断言无 `RETURNING`）+ 逐处手工改写 |
| 3 | **`run_events.seq` 在 MySQL + RC 下失去串行化**（`else` 分支实为 SQLite 语义，且**无重试**） | 高 | 中 | **V3 实测**（§8.6）：先对 **`threads_meta` 行**取 `SELECT … FOR UPDATE`（锚点缺失时先幂等建行），再读 `max(seq)`；唯一约束 + bounded retry 仅作兜底 |
| 4 | advisory lock 替换后调度器重复触发/漏触发 | 高 | 中 | 锁表 sentinel + 并发回归测试 |
| 5 | `RETURNING` 替换后序号重复/跳号（`LAST_INSERT_ID` 污染） | 高 | 中 | 两步法（`FOR UPDATE` + `UPDATE`）+ 唯一约束兜底；**禁用 `LAST_INSERT_ID(expr)`** |
| 6 | 🔴 **`JsonMatch` 的 MySQL 分支字面量大小写写错** ⇒ 谓词**恒 false**，线程置顶/归档过滤**静默失效** | **中高** | **中高** | **V6**：MySQL `JSON_TYPE()` 返回大写；用现有 `test_threads_router.py` 用例回归 |
| 7 | 时间精度截断导致 FIFO/过期判定错误 | 中高 | 高 | `DATETIME(6)` + 边界转换 + 测试 |
| 8 | RR 隔离级别导致死锁率上升 | 中高 | 高 | 显式 RC + 锁顺序审计 + 死锁重试（识别 1213） |
| 9 | 🔴 **只改驱动、不改 `("sqlite","postgres")` 守卫** ⇒ 进程**直接 `SystemExit` / `ValueError`** | **中高** | **高** | 按 §12 Goal 4 的 **6 处**清单全部扩展 `mysql`；启动 smoke test 必须覆盖 3 条 `SystemExit` 路径 |
| 10 | 🔴 **删除 Store 时漏改耦合点** —— 3 处非显然耦合：`_sqlite_utils` 被 checkpointer/health 复用、`MemoryThreadMetaStore` 依赖 `BaseStore`、`deps.py:435` 的**无条件构造** + 5 处 `store=` 管线 + `app.py` 孤儿迁移 | 中 | **中高** | 按 **§7.8 的 11 项清单**逐项改；改完跑 Gateway 启动 smoke test + checkpoint 回归 |
| 11 | 🔴 **误把"同步 Saver 不启用"扩展成"同步驱动也不要"** ⇒ `agent_storage.backend: db` 整链失效 | **中高** | 中 | 按 **§7.7** 区分二者：**保留同步驱动，只不启用同步 Saver**；并覆盖"图子进程构建 agent"这条路径的测试 |
| 12 | 🔻 **Compliance Pass 与已冻结 Saver 的冲突** —— 默认路径下 checkpoint 4 张表的**表名 / 索引名 / COMMENT 均不可改**；要改只能走 §7.3 第 4️⃣ 档，且表名改动面是 **57 处 SQL 字符串 + `LANGGRAPH_OWNED_TABLES` + 1 个单测等值断言**，并成为**永久的本地 diff**。⛔ **纯风格理由不构成 vendor 判据**（§7.3.3） | 中 | 中 | **默认不改名**，把它列为明确的**规范例外**（§15.6）。⚠️ **不要为它单独触发 vendor** |
| 13 | **删除 `mcp_tasks` / `subagent_batches` 后发现有隐藏消费者** | 中 | 低（G0 已完成并复核） | 已按证据链复核；删除后跑 Gateway 启动 + feature flag 检查（`routers/features.py`） |
| 14 | `max_allowed_packet` 不足以容纳单个 checkpoint blob | 中 | 低 | 部署 checklist 显式确认；应用侧加软上限告警；大 payload 实测（Goal 2） |
| 15 | 上游代码的 `INSERT IGNORE` 吞掉可忽略错误 | 中 | 低 | V5 中核对目标列（`LONGBLOB` / `JSON` / `VARCHAR(150)`）的取值长度确实不越界；**若确需改为显式错误处理，则走 §7.3 第 4️⃣ 档** |
| 16 | 上游代码的 `json_arrayagg` base64 线路膨胀影响吞吐 | 中 | 中 | Goal 2 的 2-E 节的大 payload 实测 + 记录 `max_allowed_packet` 上限；**若确需改 `SELECT_SQL`，则走 §7.3 第 4️⃣ 档** |
| 17 | partial index 生成列 workaround 的锁/写放大开销 | 中 | 中 | 压测写入路径 |
| 18 | 表重命名遗漏（**仅当执行 Compliance Pass**） | 中 | 中 | 启动期断言 + 一次性 grep 清单 + 单测 pin |
| 19 | Redis 重启后 SSE 静默丢失事件（gap 检测不覆盖"丢流"；SSE 路径无自动补偿） | 中 | 中 | **不在本次变更范围**（§13 的 V4 说明）；归入**独立的 Redis 变更** |
| 20 | 移除 PG 依赖后 Helm/文档/CI 不一致 | 低 | 高 | 文档与 chart 同步更新 |
| 21 | 🔻 **第三方依赖的上游漂移**（默认路径下表现为"版本固定但不再自动获得上游修复"；**仅当降级为 vendor 时**才表现为"本地改动需手工重放"） | 中 | 中 | 默认路径：`==` 精确固定版本 + 记录版本变更的复查信号（§7.4）；vendor 路径：`UPSTREAM.md` + **Gate V7** |
| 22 | 🔻 **vendor 许可合规遗漏**（未随源码保留 MIT 声明）—— **仅在降级为 vendor 时适用** | 低 | 低 | **Gate V7**：`LICENSE` 全文进 vendor 目录，`Copyright (c) 2024 Theodore Ni` 不得删改 |

---

## 15. 范围汇总

### 15.1 最小必须迁移的 Runtime 能力

| # | 能力 | 为什么不能删 |
| --- | --- | --- |
| 1 | **MySQL 异步驱动**（`asyncmy`） | Gateway / Agent Runtime / Checkpointer / RunEventStore 全走异步 |
| 2 | **MySQL 同步驱动**（`PyMySQL`） | `agent_storage.backend: db` 的 `SqlAgentStore` 在 graph subprocess 里用同步 engine |
| 3 | **Checkpoint 持久化** | LangGraph durable execution 的真相源 |
| 4 | **Application tables**（12 张 / 139 列） | 账号、线程、运行、事件、调度、反馈、项目、偏好、Agent 定义 |
| 5 | **4 处 `RETURNING` 的改写** | 租约续约、取消意图、finalize 判定、occurrence 序号 |
| 6 | **2 处事务级 advisory lock 的替换** | 事件 seq 分配、调度器全局预算 |
| 7 | **3 处 partial unique 的等价实现** | 每线程一个活跃 run、每任务一个非终态 occurrence、每个 OAuth 身份一个账号 |
| 8 | **`JsonMatch` 的 MySQL 方言分支** | 线程置顶/归档过滤 |
| 9 | **时间列 `DATETIME(6)`** | FIFO 排序与过期判定 |
| 10 | **RC 隔离级别显式设置** | 全部并发假设的前提 |
| 11 | **6 处能力闸门扩展 `mysql`** | 否则进程直接拒绝启动 |
| 12 | **独立 migration chain + 运维执行的 `upgrade(head)` 迁移产物** | 建库（**Runtime 不做**） |
| 13 | **Checkpoint Schema 的独立迁移产物**（`database/mysql/checkpoint/`，22 条线性 + 版本表守卫） | checkpoint 4 张表的创建（**Runtime 不做 `setup()`**） |
| 14 | **Runtime 的只读 Schema / 版本校验**（6 项 + fail closed） | 无 DDL 权限账号下的就绪判定；防止带着过期 Schema 启动 |
| 15 | **`mysql` health 探针** | 就绪判定 |
| 16 | **`run_events.seq` 的串行化方案** | 事件序号正确性（V3 已实测确定具体方案） |

### 15.2 可以直接删除的能力（**已由 Goal 0 完成**）

| # | 能力 | 依据 |
| --- | --- | --- |
| 1 | **Channel / GitHub Webhook**（5 张表 + 14 模块 + 14 测试） | `feature-inventory` Phase 3 既定工作；迁移前置 |
| 2 | **`mcp_tasks`**（1 表 45 列 + 9 索引 + 4 条租约通道 + 10 测试） | 无当前生产消费者（§2.3） |
| 3 | **`subagent_batches` / `subagent_batch_items`**（2 表 40 列 + 7 索引 + 10 测试） | 无当前生产消费者（§2.3） |
| 4 | **LangGraph Store**（`runtime/store/*` + 5 处 `store=` 管线 + 孤儿迁移） | DB 模式下"构造了但从不读写"（§7.8） |
| 5 | **`MemoryThreadMetaStore` 对 LangGraph `BaseStore` 的依赖**（⚠️ **只删依赖，不删能力** —— 改为约 35 行内部 dict 实现） | §7.8 决策 6 |
| 6 | **Checkpoint blob 的对象存储溢出设计** | §7.6 |
| 7 | **JSON → TEXT 的批量改写与 5 个 scalar 拆列** | §6.5 |
| 8 | **FK → 应用层级联 + 孤儿巡检** | §6.6 |
| 9 | **`pgvector` / Vector 讨论** | Store 删除后彻底消失 |
| 10 | **`FOR SHARE` 归属校验的重新设计** | 随 `mcp_tasks` 删除 |
| 11 | **通用 multi-chain migration framework** | §11.2 |
| 12 | **`acquire_txn_lock` / `allocate_sequence` / `conditional_upsert` 抽象层** | §12 G1 横切约束 |
| 13 | **双数据库测试参数化矩阵** | §12 Goal 4 |
| 14 | **K8s / Helm 的 MySQL 部署形态** | §3.4 |

### 15.3 可以延后的能力

| # | 能力 | 延后到哪里 |
| --- | --- | --- |
| 1 | **Optional Compliance Pass**（`ag_` 前缀 / 索引命名 / 中文 COMMENT / 约束补名） | Core 稳定后，作为**独立变更**（**不是正式 Goal**，§15.9） |
| 2 | **Redis checkpoint cache** | **移出本迁移方案**，独立变更 |
| 3 | **Redis 鉴权读缓存** | **移出本迁移方案**，独立变更 |
| 4 | **StreamBridge 的 durable replay fallback** | **移出本迁移方案**，独立变更 |
| 5 | **Sandbox ownership 的 Redis 持久化决策** | **移出本迁移方案**，独立变更 |
| 6 | **索引优化**（冗余清理 / tuning / 合并 / 覆盖索引） | MySQL 上线后按真实负载 |
| 7 | **JSON 过滤的函数索引 / 拆列** | MySQL 上线后按真实慢查询 |
| 8 | **Checkpoint blob 的对象存储溢出** | 当实测出现 `max_allowed_packet` 逼近或存储成本问题时 |
| 9 | **K8s / Helm 的 MySQL 部署** | 出现真实 K8s 生产需求时 |

### 15.4 可以复用第三方实现的能力

| # | 能力 | 复用对象 |
| --- | --- | --- |
| 1 | **async CheckpointSaver**（`aget_tuple` / `alist` / `aput` / `aput_writes` / `adelete_thread`） | **固定版本依赖** `langgraph-checkpoint-mysql==3.0.0` 的 `AsyncMySaver`（**asyncmy 驱动，正好匹配**） |
| 2 | **checkpoint 4 张表的 DDL**（**22 条** `MIGRATIONS` 的最终形态） | 同上（`base.py:25-149`）—— 但**生产由运维执行其派生产物**（§11.4），**不是 `setup()`** |
| 3 | **checkpoint 的序列化与 blob 分流**（primitive 内联 + 其余进 blob） | 同上（`aio_base.py:206-277`） |
| 4 | **pending writes / sends 的处理** | 同上（`utils.py:29-58`） |
| 5 | **`checkpoint_ns` 的哈希主键方案**（解决索引键长问题） | 同上（`base.py:100-148` 的三条 `ALTER`） |
| 6 | **连接池的鸭子类型识别**（`hasattr(conn,"acquire")`） | 同上（`_ainternal.py:73-83`） |
| 7 | **应用侧 JSON 序列化**（`json.dumps(ensure_ascii=False)` + 类型标记） | 项目**已有**（`persistence/engine.py:25-27`、`runtime/events/store/db.py`） |
| 8 | **Object Storage 端口**（Artifacts / Uploads / Tool 结果） | 项目**已有**（`object_storage/port.py`） |
| 9 | **独立 alembic chain + 独立 `version_table` 的先例** | 项目**已有**（`migrations/AGENTS.md:128`） |
| 10 | **`sa.JSON` 的原生 `JSON` 映射** | SQLAlchemy 方言层（无需任何改动） |
| 11 | **InnoDB FK 与级联删除** | MySQL 原生 |
| 12 | **`LONGBLOB`** | MySQL 原生 |

> 🔻 **默认路径下"不 vendor"的东西自动不存在**：因为整包作为依赖引入，
> 我们**只导入 `langgraph.checkpoint.mysql.asyncmy.AsyncMySaver`**，
> `PyMySQLSaver` / `AIOMySQLSaver` / `ShallowAsyncMySaver` / 整个 `langgraph/store/mysql/`
> **都不进入执行路径**，也**不需要我们维护**。
>
> ⚠️ **仅当 §7.3 第 4️⃣ 档被触发时**，才需要裁剪：
> 只取 `base` / `aio_base` / `asyncmy` / `utils` / `_ainternal` **5 个文件（约 1197 行 = 整包 3830 行的 31%）**，
> 并丢弃 `pymysql.py`（同步 Saver）、`aio.py`（aiomysql）、`shallow.py`（899 行，已 deprecated）、
> 上游 `__init__.py`（442 行）、`_internal.py`（异步链零引用）、`langgraph/store/mysql/`。

### 15.5 真正属于 MySQL 技术兼容性的工作

见 §4.8 第 1–11 项 + **§4.9 的静默失效面**。摘要：

| # | 工作 | 数量 |
| --- | --- | --- |
| 1 | `RETURNING` 改写（🔴 **SQLAlchemy 会静默编译出来**，§4.9-A） | 4 处 |
| 2 | advisory lock 替换（🔴 **V3 已实测失败，改为锁 `threads_meta` 行**） | 2 处 |
| 3 | partial unique → 生成列唯一索引（🔴 **只有 2 处**，OAuth 不需要，§4.4） | **2** 处 |
| 4 | `JsonMatch` 方言分支（🔴 **`JSON_TYPE()` 大写**） | 1 个文件 |
| 5 | 时间列 `DATETIME(6)`（🔴 **`DateTime(timezone=True)` 静默变 `DATETIME` 且丢亚秒**） | 29 列 |
| 6 | UTC-naive 写入边界 | 应用层统一 |
| 7 | 显式 RC 隔离级别 | 1 处配置 |
| 8 | 🔴 **错误码 `1062` 判别改写**（三个判别函数在 MySQL 上全部失效，§4.9-D） | `auth/repositories/sqlite.py` |
| 9 | `hashtext()` → 应用层 `sha256` | 1 处 |
| 10 | 能力闸门扩展 `mysql` | 6 处 |
| 11 | 驱动 `Literal` 扩展 | 2 处 |
| 12 | 🔴 **`date_trunc()` / `EXTRACT(epoch)` 改写**（同样静默编译，§4.9-A） | 按实际命中数 |
| 13 | 🔴 **生产迁移执行模型**（两份迁移产物 + Runtime 只读校验，§11） | 新增工作面 |

### 15.6 仅属于企业 Schema compliance 的工作（**不进入正式 Goal**）

见 §4.8 第 12–18 项与 §2.7 的第二层。**主文档只写边界，详细规范移入附录或独立文档**（§2.7 关键约束 4）。摘要：

| # | 工作 | 数量 | 与 Core 的冲突点 |
| --- | --- | --- | --- |
| 1 | 表名 `ag_` 前缀 | 12 张应用表 + 4 张 checkpoint 表 | 🟡 checkpoint 表名**改不了**（上游包的 SQL 里硬编码，**默认路径下不可改**）⇒ **列为规范例外**。⛔ 纯风格理由**不构成 vendor 判据**（§7.3.3） |
| 2 | 索引命名 `uk_` / `idx_` | 31 个 + checkpoint 表的 4 个 | 🟡 同上 |
| 3 | 中文 TABLE / COLUMN COMMENT | 全量 | ⚠️ checkpoint 4 张表**加不了 COMMENT**（同上），列为例外 |
| 4 | 未命名唯一约束补名 | 1 处 | 无 |
| 5 | 禁止 `JSON` 列 | 11 列 + checkpoint 3 列 | 🔴 **不采纳**（MySQL 原生可用） |
| 6 | 禁止 `BLOB` / `LONGBLOB` | 2 列 | 🔴 **不采纳**（`LONGBLOB` 是正确容器） |
| 7 | 禁止 FK | 1 处 | 🔴 **不采纳**（InnoDB 原生可用） |

> 🔴 **关键判断（已冻结）**：第 1–3 项的 checkpoint 部分**不再触发 vendor** ——
> 表名 / 索引名 / COMMENT 都属于纯风格，已被明确排除在 vendor 判据之外（§7.3.3 / §7.4）。
> **它也不构成 Core Migration 的前置条件** ——
> 按 §2.7，Core 用"能否正确运行"验收，Compliance 用"是否满足规范"验收。
> ⇒ **先按默认路径上线，把表名 / COMMENT 的合规问题留给 Compliance Pass 独立决策**（§15.10 Q2）。

### 15.7 CheckpointSaver 的接入方案

```
选型：🔴 已冻结 —— langgraph-checkpoint-mysql[asyncmy]==3.0.0（MIT，纯 Python，>=8.0.19）
      ⛔ 不是"候选"，不讨论"是否使用第三方 Saver"

引入：精确版本直接依赖
      · 声明：harness/pyproject.toml 的 [project.optional-dependencies] 新增 mysql extra
      · 只导入 langgraph.checkpoint.mysql.asyncmy.AsyncMySaver
      · ✅ 无本地 fork、无需跟踪上游 diff、无需改 import
      · ⛔ 默认不 vendor、不复制约 1200 行第三方源码进仓库

依赖：langgraph-checkpoint-mysql[asyncmy]==3.0.0
                                        🔴 精确固定（§7.4 附 C）
      orjson>=3.10.1                    ⚠️ 随上游声明一并安装（包内零命中，无需处理）
      asyncmy>=0.2.10                   ✅ 新增（异步 Application / Checkpointer 路径）
      PyMySQL>=1.1.1                    ✅ 新增（同步 SqlAgentStore 路径，**必须保留**，§7.7）
      langgraph-checkpoint 4.1.1        ✅ 已装
      typing-extensions 4.15.0          ✅ 已装
      ⛔ 不启用同步 MySQL CheckpointSaver

表：checkpoints / checkpoint_blobs / checkpoint_writes / checkpoint_migrations
    · 🔴 生产由运维 / DBA 执行迁移产物创建
      （database/mysql/checkpoint/，22 条线性脚本 + checkpoint_migrations 版本表守卫）
    · 🔴 产物与 3.0.0 版本绑定；升级 Saver 时走 §11.4.4 的五步流程
    · ⛔ Runtime 不调用 setup()、不建表、不改表、不自动升级 Schema
    · 不进 MySQL 链的任何 revision
    · checkpoint_ns VARCHAR(2000)，PK 用 checkpoint_ns_hash BINARY(16)（普通列）
    · blob 列 LONGBLOB（单库直存，无对象存储溢出）

代码：runtime/checkpointer/async_provider.py 新增 mysql 分支（约 20 行）
    · create_mysql_pool(...) → AsyncMySaver(conn=pool)
    · 🔴 然后调用 verify_checkpoint_schema(...) 做只读校验，**不调 setup()**
    · _ainternal.get_connection 鸭子类型识别池（hasattr(conn, "acquire")）
    · ⚠️ saver 必须在**运行中的事件循环内**构造（__init__ 调 asyncio.get_running_loop()）
    · checkpoint_channel_mode=full ⇒ 不挂 CachedHistorySaver

配置：database_config.py:143 / checkpointer_config.py:9 追加 "mysql"
      health.py 的探针 Literal 扩展

验证：V5（§7.3.1 的完整清单 + 三处实现细节 + 现有回归测试，在真实 8.0.24 上）
      ⚠️ V5 是 compatibility / correctness Gate，**不是 selection Gate**；
         失败时按 §7.3.2 的固定阶梯处理，**不重新选型**
      §11.9 的 A 组（迁移产物）+ B 组（无 DDL 权限账号可正常启动）
```

#### 15.7b fallback 路径：vendor **同一个 3.0.0** + 最小 patch（**仅 §7.3 第 4️⃣ 档触发时**）

> ⚠️ **这不是"降级为另一个方案"，而是对已冻结基线做本地 patch。**
> ⛔ 不换包、不重新选型。

```
触发：§7.3 的第 1️⃣–3️⃣ 档（接入方式 / adapter-wrapper / subclass）已被逐一排除，
      且确实必须修改 package 内部 SQL / 私有实现

引入：vendor 上游 langgraph-checkpoint-mysql **3.0.0** 源码
      · 落点 runtime/checkpointer/mysql/（deerflow 包内 ⇒ 自动进 wheel）
      · 复制 5 个文件：base.py / aio_base.py / asyncmy.py / utils.py / _ainternal.py
      · 改 6 处 import（✅ 已实测：6 处全部命中，改写后**零残留上游 import**）
      · 裁剪 asyncmy.py 的 ShallowAsyncMySaver
      · 自写 __init__.py / pool.py；随附 LICENSE（MIT）+ UPSTREAM.md
      ⛔ 不复制：pymysql.py / aio.py / shallow.py / _internal.py / 上游 __init__.py
                / langgraph/store/mysql/**
      · 依赖：从 mysql extra 删掉 langgraph-checkpoint-mysql 那一行；
              asyncmy / PyMySQL **不变**（无论如何都要装）
      · 🔴 版本仍是 3.0.0 ⇒ MIGRATIONS 基线不变（22 条）
      · 表 / 代码 / 配置 / 验证：**与默认路径完全一致**
```

#### 15.7c 缺口出现时的处置阶梯（**固定阶梯，不得跳档**）

> 🔴 与 §7.3.2 是同一套阶梯。**实现基线永远是 `langgraph-checkpoint-mysql==3.0.0`。**

```
1️⃣ 确认是不是项目接入方式的问题
    · 池没正确传入（_ainternal.get_connection 要求 hasattr(conn, "acquire")）
    · saver 不是在运行中的事件循环内构造（__init__ 调 asyncio.get_running_loop()）
    · checkpoint_channel_mode 不是 full
    · 配置分支 / Literal 漏改；Schema 校验未前置
    ⇒ 修接入代码，**不动上游**。多数问题停在这一档。

2️⃣ adapter / wrapper（不改上游源码，包一层）
    · 参数转换 / 结果后处理 / 重试与降级

3️⃣ subclass 覆写（精确版本依赖下即可，不需要 vendor）
    class DeerFlowMySQLSaver(AsyncMySaver):
        # MIGRATIONS / UPSERT_*_SQL 是类属性（base.py:246-251）⇒ 直接覆盖
        # SELECT_SQL 走 _select_sql() ⇒ 覆写该静态方法

4️⃣ vendor **同一个 3.0.0** + 最小 patch（改动 < 50 行）
    # 前提：缺口在模块级 SQL 常量 / 私有辅助函数上，前三档确实够不到
    # 表结构：沿用上游 MIGRATIONS（已实测可运行的 MySQL DDL 序列）
    # blob：LONGBLOB 单库直存（不变）
    # SELECT_SQL / UPSERT_*：直接改 base.py 里的常量
    # 事务：沿用 _cursor(pipeline=True) 的 begin/commit/rollback + asyncio.Lock
    # 每次改动都登记进 UPSTREAM.md 的"本地改动清单"

5️⃣ architecture blocker（唯一允许跳出"3.0.0 基线"的情形）
    # 仅当出现结构性、无法修复的 correctness 问题
    # ⇒ 档位回落（§1.4），重新评估迁移路径
```

**关键点**：fallback **不需要**"从零设计表结构 + 重写全部 SQL + 自研 `setup()` 迁移链"。
**若走到第 5️⃣ 档，档位必须回到 `Feasible with significant changes`。**

### 15.8 复杂度结论

| 项 | 结论 |
| --- | --- |
| **CheckpointSaver 选型** | 🔴 **已冻结**：`langgraph-checkpoint-mysql[asyncmy]==3.0.0`；**默认精确版本直接依赖**（§0） |
| **目标档位** | **`Moderate`**（条件：**V5 兼容性 / 正确性验收通过**） |
| **V5 未做前的已证实档位** | `Feasible with significant changes` |
| **V5 失败时的档位** | 先走 §7.3 / §15.7c 的**前四档**，**档位不变**；只有走到第 5️⃣ 档才回落到 `Feasible with significant changes` |
| **性质** | MySQL fresh-cutover / PostgreSQL backend replacement |
| **一句话** | 最难的那件事（自研 CheckpointSaver）已由"**精确版本复用成熟实现**"替代（选型已冻结）；**依赖面只增 1 个包**（+ 驱动本身）；G0 已完成，范围收缩了 38% 的列与 45% 的 `FOR UPDATE`；🔴 **新增一条结构性硬约束** —— **生产 Runtime 零 DDL**，Schema 由运维执行、Runtime 只验证；剩下的是一批**机械但需要仔细**的方言适配与并发验证 |

### 15.9 Goal 拆分（**6 个**）

| Goal | 内容 | 退出条件 | 前置 Gate | 可独立交付 |
| --- | --- | --- | --- | --- |
| **Goal 0 —— Runtime 范围清理** ✅ **DONE**（`a55e5734`） | 删除 Channel / GitHub Webhook、`mcp_tasks`、`subagent_batches`、LangGraph Store（含 `MemoryThreadMetaStore` 改内部 dict） | ✅ 已满足：① Gateway 正常启动；② 普通 MCP 可用；③ 普通 SubAgent `task` 可用；④ 最终 ORM 表集稳定（**12 表 / 139 列**）；⑤ 重新扫描 PG 专属依赖 | — | ✅ **已完成** |
| **Goal 1 —— MySQL 兼容性与风险 Gate** | **G1-A（V1）** MySQL 独立链；**G1-B（V5）** 对**已冻结**的 `langgraph-checkpoint-mysql==3.0.0` 做兼容性 / 正确性验证；**G1-C** 生产迁移执行模型（两份产物 + zero DDL + 只读校验 + fail closed）。✅ V3 / V6 / V2 已关闭 | 三项各自有明确结论（V1/V5 通过或失败 + 处置；G1-C 设计定稿） | — | ✅ |
| **Goal 2 —— MySQL 持久化底座** | `asyncmy`；必需的 `PyMySQL`；Application MySQL 连接；**Async CheckpointSaver**（已冻结的 3.0.0）；**Runtime Schema / 版本校验**；`0001_mysql_baseline`；**Checkpoint 迁移产物**；Dev/Test 迁移支持 | 🔴 **生产 Runtime 不执行任何 DDL**；§11.9 的 A 组 + B 组验收通过 | **V1 / V5** | ✅ |
| **Goal 3 —— 应用 SQL 与并发迁移** | `RETURNING`；advisory lock；partial unique（**2 处**）；`JsonMatch`；`DATETIME(6)` / UTC；`FOR UPDATE` / `SKIP LOCKED`；MySQL `1062` / `1213`（**含 §4.9-D 的判别改写**）；`run_events.seq`；Scheduler 并发 | §4.9-A 的四类静默失效构造全部清除；并发回归通过 | V3（✅）/ V6（✅） | ✅ |
| **Goal 4 —— 全量 MySQL 集成验证** | 在**真实 MySQL 8.0.24** 上验证：Gateway；agent 对话；checkpoint resume；interrupt / retry / rollback；Scheduler；多实例；长会话；**生产化权限模型** | 必须包含：① **应用 Runtime 用"无 DDL 权限"账号正常启动并运行**；② **未执行迁移 → fail closed**；③ **已执行迁移 → `ready`**；④ **Runtime 无任何自动建表 / 改表** | Goal 2 / Goal 3 | ✅ |
| **Goal 5 —— 删除 PostgreSQL** | MySQL 全链稳定后：删 PG 驱动；Postgres Checkpointer；PG migration legacy；PG bootstrap legacy；PG 专属测试；PG 配置；PG health；PG 部署；PG 兼容分支 | 最终 Runtime **只保留 MySQL** | Goal 4 + 稳定期 | ✅ |

**⛔ 不在这 6 个 Goal 里的**：

| 项 | 去处 |
| --- | --- |
| **Optional Compliance Pass**（`ag_` 前缀 / 索引命名 / COMMENT） | **不是正式 Goal** —— 主文档只写边界（§2.7 / §15.6），详细规范移入附录或独立文档。**不阻塞迁移** |
| **Redis 的一切**（checkpoint cache / delta 缓存 / sandbox ownership 持久化） | **移出本迁移方案**，作为独立跟进设计（§2.5） |
| **对象存储溢出 checkpoint blob** | 不在本次迁移范围；按真实 payload 独立评估（§7.6） |
| **Kubernetes / Helm 的 MySQL 部署形态** | 不在本次迁移范围（§3.4） |

> **Goal 0 已完成** ⇒ **Goal 1 是当前唯一的起点**（G1-A / G1-B / G1-C 三项可并行推进）。
> **Goal 2 中的 CheckpointSaver 接入必须最先做**（最高风险优先验证），但**必须先过 V1 / V5**。
> **Goal 5 之后不再保留 PG 兼容分支** —— 不要留"以防万一"的双分支。

### 15.10 剩余真正需要项目负责人确认的问题

> 🔴 **判定标准（严格执行）**：凡是**能通过代码调查、MySQL 8.0.24 实测、V1 / V5 测试
> 或兼容性测试**回答的，**一律不列在这里** —— 那些已在正文给出结论。
> **下面只留真正需要业务 / 组织决策的项。**

| # | 问题 | 影响 | 建议 |
| --- | --- | --- | --- |
| **Q1** | **MySQL 链的 `version_table` 用默认 `alembic_version` 还是 `ag_alembic_version`？** | 必须在 `0001_mysql_baseline` 落地时确定，事后改名要改链根 | ✅ 建议默认 `alembic_version`（两条链在不同 database，天然隔离）；若组织强制 `ag_` 前缀则用 `ag_alembic_version` |
| **Q2** | **是否接受"checkpoint 4 张表名不加 `ag_` 前缀、不加 COMMENT"，即把它们列为一条明确的规范例外？** | 仅影响 Optional Compliance Pass 的清单完整性 | ✅ 建议接受例外。⚠️ **注意：这条已经不触发 vendor** —— 纯风格理由不构成 vendor 判据（§7.3.3 / §7.4） |
| **Q3** | **Compliance Pass 的交付时点**（与 Core 同批 / Core 稳定后独立）？ | 决定失败归因是否可分离 | ✅ 建议 Core 稳定后独立交付 |
| **Q4** | **是否接受"本期不新增 Kubernetes / Helm 的 MySQL 部署形态"？** | 决定部署工作范围 | ✅ 建议接受（当前真实目标只有 compose + external DSN） |

> ✅ **已由调查 / 实测 / 已冻结决策给出结论、不再需要负责人拍板的项**：
>
> | 原问题 | 现结论 | 依据 |
> | --- | --- | --- |
> | **是否使用 `langgraph-checkpoint-mysql`？** | 🔴 **使用**（选型已冻结） | §0 |
> | **直接依赖还是 vendor？** | 🔴 **默认直接依赖**；vendor 仅为同版本 patch fallback | §0 / §7.4 |
> | **是否接受第三方 Saver？** | 🔴 **接受**（MIT / 纯 Python / MySQL ≥ 8.0.19） | §0 / §7.2 |
> | **V5 通过后再决定用不用？** | 🔴 **不再适用** —— V5 是 compatibility / correctness Gate，不是 selection Gate | §7.3.1 |
> | **是否接受 checkpoint 4 张表不进 MySQL 链、由运维执行上游派生迁移产物？** | **接受** —— 这是"生产 Runtime 零 DDL"的必然结果 | §11.0 / §11.4 |
> | `mcp_tasks` / `subagent_batches` 是否有生产使用计划？ | **已无意义** —— 两模块已随 G0 删除 | §12 Goal 0 |
> | 是否接受 `LONGBLOB` 单库直存（不做对象存储溢出）？ | **接受** —— 上游 schema 本来就是 `LONGBLOB`（已实测导出） | §7.6 / §11.4.1 |
> | 是否接受保留 `sa.JSON` → 原生 `JSON`？ | **接受** —— MySQL 原生可用，无正确性问题 | §6.5 |
> | 是否接受保留 FK？ | **接受** —— InnoDB 原生可用 | §6.6 |
> | 是否接受"不做双数据库测试参数化矩阵"？ | **接受** —— PG 最终删除 | §12 Goal 4 |
> | `MemoryThreadMetaStore` 怎么处理？ | **保留能力，改内部 dict** | §7.8 决策 6 |
> | `idx_users_oauth_identity` 是否需要生成列？ | **不需要** —— 已实测语义等价 | §4.4 |
> | `run_events.seq` 能否串行化？ | **不能；修法已实测有效**（锁 `threads_meta` 行 + 幂等建锚点） | §8.6 |
> | `checkpoint_ns` 真实最大长度？ | **不再是问题** —— 主键用固定 16 字节 hash | §11.4.3 |
> | checkpoint 表的锁锚点表名？ | **`threads_meta`（PK `thread_id`）**，且需自保证锚点存在 | §8.6 |

> ⛔ **以下问题不得再出现在 Open Questions / Owner Decisions / Architecture Decisions Pending /
> G1 待确认事项中**（已冻结或已由代码事实解决）：
> ```
> · 是否使用 langgraph-checkpoint-mysql？
> · 直接依赖还是 vendor？
> · 是否接受第三方 Saver？
> · 候选 Saver 有哪些？
> · V5 通过后是否采用？
> ```

---

## 16. 证据清单

| 类别 | 路径 / 方法 |
| --- | --- |
| 🔴 **G0 完成状态与当前 HEAD** | `git log -1 --format='%H %ci %s'` → **`a55e573434f69f2c333cc057f92ac8f73cba1b92`** / `2026-09-18 09:03:18 +0800` / `refactor(runtime): remove channels, background MCP tasks, subagent batches, and LangGraph Store`；`git status --porcelain` **空**；分支 `feat_portal`。审计记录：`docs/architecture/mysql-goal0-audit.md` |
| 🔴 **当前 Application Schema 反射** | `backend/.venv/bin/python` 逐个 import `deerflow.persistence.*.model` → 读 `Base.metadata.tables`。结果：**12 表 / 139 列**；`DateTime(timezone=True)` **29**；`sa.JSON` **11**；FK **1**（`user_preferences.user_id → users.id`）；索引 **31**。逐表：`runs` 31 / `scheduled_tasks` 23 / `scheduled_task_runs` 16 / `threads_meta` 10 / `run_events` 10 / `users` 9 / `personal_access_tokens` 9 / `feedback` 8 / `projects` 8 / `agents` 7 / `managed_subagents` 5 / `user_preferences` 3 |
| 🔴 **并发原语复扫** | `with_for_update` 按**含该关键字的行数**统计（生产代码，排除 `tests/` 与 `migrations/versions/`）：`scheduled_task_runs/sql.py` 8 + `scheduled_tasks/sql.py` 11 + `thread_meta/sql.py` 6 + `runtime/events/store/db.py` 1 + `run/sql.py` 1 + `projects/sql.py` 1 = **28** ✅；`RETURNING` = `run/sql.py` 3 + `scheduled_task_runs/sql.py` 1 = **4** ✅；`SKIP LOCKED` = `scheduled_tasks/sql.py` **2** ✅；事务级 advisory lock = `scheduled_task_runs/sql.py` 1 + `runtime/events/store/db.py` 1 = **2** ✅ |
| 🔴 **PG 耦合面复扫** | 含 `postgres`（大小写不敏感）的 `.py` 文件：`packages/harness/deerflow` **39** + `app/` **5** + `tests/` **47** = **91**（生产代码 **44**） |
| 🔴 **数据库能力闸门复扫** | 6 处会拒绝 MySQL 启动的守卫：`app/gateway/deps.py:79`（`backend != "postgres"` → `SystemExit`）· `deps.py:92`（同）· `deps.py:128`（`not in ("sqlite","postgres")` → `SystemExit`）· `persistence/agents/__init__.py:47`（`ValueError`）· `persistence/managed_subagents/__init__.py:32`（`ValueError`）· `app/gateway/health.py:229`（返回 `DATABASE_UNREACHABLE`）。⚠️ 前两处是 **`!= "postgres"`** 形式，不是元组形式。另有 **2 处 `Literal`** 需扩展：`config/database_config.py:143`、`config/checkpointer_config.py:9` |
| 🔴 **锁锚点表名** | `persistence/thread_meta/model.py:13-16`：`__tablename__ = "threads_meta"`，**PK `thread_id: String(64)`**。⚠️ 仓库中**不存在**名为 `threads` 的表 ⇒ `SELECT id FROM threads ...` 是错的（§8.6） |
| 🔴 **锚点存在性缺口** | `app/gateway/services.py:190-226` `_ensure_thread_metadata()` 在 run admission 创建 `threads_meta` 行；但 `services.py:1628-1633` 的失败日志带 `(non-fatal)`，`services.py:1662-1665` 注释明确 *"Continue through run_agent even after metadata abort, timeout, or strict verification failure"*；且全仓**无调用点**传 `require_existing_thread=True`（`services.py:1429` 默认 `False`）⇒ **锚点行不保证存在**，锁方案必须自保证 |
| 🔴 **G0 残留（易误判）** | `persistence/{mcp_tasks,subagent_batches,channel_connections,webhook_delivery}/` 目录下**只剩未跟踪的 `__pycache__`**（源码已删，`persistence/models/__init__.py` 不再 import）；`persistence/migrations/versions/0011_mcp_tasks.py` 与 `0016_subagent_batches.py` **仍被 git 跟踪**，属不可变的 PG 历史链，只作审计、**不由 Gateway 回放** |
| 依赖实测 | `backend/.venv/bin/python -c "importlib.metadata.version(...)"`（`langgraph` 1.2.9 / `langgraph-checkpoint` **4.1.1** / `langgraph-checkpoint-postgres` **3.1.1 已安装** / `langgraph-checkpoint-sqlite` 3.1.1 / `sqlalchemy` 2.0.49 / `asyncpg` 0.31.0 / `psycopg` 3.3.3 / `alembic` 1.18.4） |
| **第三方包元数据** | `curl https://pypi.org/pypi/langgraph-checkpoint-mysql/json` → 3.0.0（2026-01-23），`requires_dist` = `langgraph-checkpoint>=2.1.2` / `orjson>=3.10.1` / `typing-extensions>=4.12.2`；extras `pymysql` / `aiomysql` / **`asyncmy`**；`License-Expression: MIT`，`Author-email: Theodore Ni`，`Requires-Python: >=3.10`，`Repository: https://www.github.com/tjni/langgraph-checkpoint-mysql` |
| **第三方包源码** | `curl` 下载 `langgraph_checkpoint_mysql-3.0.0-py3-none-any.whl`（38,009 字节）→ 解包；`langgraph/checkpoint/mysql/` 共 10 个 py 文件 / 2895 行，加 `langgraph/store/mysql/` 7 个文件后共 **16 个 py 文件 / 3830 行** |
| **vendor 文件集（逐个文件行数）** | 逐字统计：`__init__.py` 442 / `aio_base.py` 544 / `base.py` 414 / `asyncmy.py` 115 / `pymysql.py` 114 / `aio.py` 117 / `shallow.py` 899 / `utils.py` 86 / `_ainternal.py` 83 / `_internal.py` 81；**必需 5 文件合计 1242 行**（裁剪 `asyncmy.py` 后约 1197 行），占整包 **31%** |
| **vendor 的 import 改写点** | `grep -nE "^\s*(from\|import) "` 逐文件：`base.py:16`、`aio_base.py:21,22,23`、`asyncmy.py:13,14`（共 **6 处**，其中 `asyncmy.py:14` 是删除）；`shallow.py:20,21,22` / `pymysql.py:13,14,15` / 上游 `__init__.py:21,22,23` / `aio.py:12,13,14` **均不在 vendor 集内** |
| **`_internal` 引用分布（证明可丢弃）** | `grep -c "_internal"` 逐文件：`__init__.py` **7**、`shallow.py` **6**、`pymysql.py` **2**；`base.py` / `aio_base.py` / `asyncmy.py` / `utils.py` / `_ainternal.py` **全部 0** ⇒ 异步链零引用 |
| **`orjson` 零命中** | `grep -rn "orjson" langgraph/checkpoint/mysql/ langgraph/store/mysql/` → **0 命中**（虽然 `requires_dist` 声明了 `orjson>=3.10.1`）⇒ vendor 后**不需要该依赖** |
| **MIT 许可全文** | `dist-info/licenses/LICENSE`（1068 字节）→ `MIT License` / `Copyright (c) 2024 Theodore Ni` ⇒ vendor 时必须随源码保留 |
| **checkpoint 表名引用次数（改名成本）** | vendored 5 文件内逐名统计：`checkpoints` **32**、`checkpoint_writes` **12**、`checkpoint_blobs` **10**、`checkpoint_migrations` **3** ⇒ 合计 **57 处 SQL 字符串** |
| **`LANGGRAPH_OWNED_TABLES` 与其单测** | `persistence/migrations/_env_filters.py:30-37`（frozenset 含 4 个 checkpoint 表名，**默认无需改动**）；`backend/tests/test_persistence_migrations_env.py:60-63` 对该集合有**等值断言** ⇒ 若改名必须同步改 |
| **打包与 extra 落点** | `harness/pyproject.toml:82-83` `[tool.hatch.build.targets.wheel] packages = ["deerflow"]` ⇒ 放 `deerflow/` 下的 vendor 代码**自动进 wheel**；`:56-63` 的 `[project.optional-dependencies] postgres` extra 是新增 `mysql` extra 的并列位置 |
| **驱动安装现状** | `backend/.venv/bin/python -c "importlib.metadata.version(...)"` → `typing-extensions` **4.15.0 已装** ✅；`asyncmy` **未装**（`PackageNotFoundError`）⇒ 它是本次要新增的驱动 |
| **Saver 构造的 loop 约束** | `aio_base.py:34-43`：`self.loop = asyncio.get_running_loop()`；`:403-544` 的 5 个**同步方法不是 stub**，而是 `asyncio.run_coroutine_threadsafe(..., self.loop)` 转发 ⇒ 必须在运行中的循环内构造 |
| 🔴 **上游只给单连接，不给池** | `asyncmy.py:39-62` 的 `from_conn_string` 内部是 `async with connect(**cls.parse_conn_string(conn_string), autocommit=True) as conn` —— **单连接**；`asyncmy.py:21-37` 的 `parse_conn_string` 是**公开静态方法**，可直接复用做 DSN 解析 ⇒ 项目需自建 `pool.py`（`asyncmy.create_pool`），**两条路径都需要** |
| **第三方包运行语义** | `aio_base.py:49-73`（`setup`）、`:75-139`（`alist`）、`:141-204`（`aget_tuple`）、`:206-277`（`aput`）、`:279-310`（`aput_writes`）、`:312-331`（`adelete_thread`）、`:333-356`（`_cursor` 事务）、`:358-401`（`_load_checkpoint_tuple`）；`base.py:25-149`（`MIGRATIONS` **22 条** —— 由 `ast` 解析常量长度实测）、`:151-216`（`SELECT_SQL` / `SELECT_PENDING_SENDS_SQL`）、`:218-243`（`UPSERT_*`）、`:350-359`（`get_next_version`）、`:361-403`（`_search_where`）；`_ainternal.py:73-83`（**池的鸭子类型识别**）；`utils.py:10-14`（`decode_base64_blob`）、`:83-86`（`mysql_mariadb_branch` → `/*!50700 mysql*//*M! mariadb*/`，**MySQL 只执行第一段**） |
| 🔴 **真实 MySQL 8.0.24 容器实测** | `docker run mysql:8.0.24`（`ServerVersion 8.0.24`）。**全部结论来自真实服务端执行，不是推断** |
| **Checkpoint 迁移链实测执行** | 用 `ast` 从 wheel 的 `base.py` 取 `MIGRATIONS`（**22 条**）→ 渲染为 SQL → **在真实 8.0.24 上逐条执行，全部成功** → `mysqldump --no-data` 导出最终 schema：4 张表；PK 分别为 `(thread_id, checkpoint_ns_hash, checkpoint_id)` / `(thread_id, checkpoint_ns_hash, channel, version)` / `(thread_id, checkpoint_ns_hash, checkpoint_id, task_id, idx)`；`checkpoint_ns_hash binary(16) NOT NULL` **是普通列**；`checkpoint_ns varchar(2000)`；`blob longblob`；4 个无前缀索引名；`metadata json NOT NULL DEFAULT (_latin1'{}')` |
| 🔴 **迁移脚本不可重复执行（实测）** | 同一组 22 条语句**再跑一遍** → `ERROR 1061 Duplicate key name 'checkpoints_thread_id_idx'`（第 51 行）⇒ **产物必须是"线性 + `checkpoint_migrations` 版本表守卫"，不是幂等脚本** |
| **OAuth 唯一索引语义（实测）** | `UNIQUE (oauth_provider, oauth_id)`：4 行 `(NULL,NULL)` 共存 ✅、3 行 `('github',NULL)` 共存 ✅、2 行 `(NULL,'oid-1')` 共存 ✅、重复 `('github','oid-9')` → **`ERROR 1062`** ✅ ⇒ **语义完全等价，不需要生成列** |
| **生成列 workaround（实测）** | `CASE WHEN status IN (...) THEN thread_id ELSE NULL END STORED` + `UNIQUE`：3 个终态 + 1 个活跃共存（4 行）✅；第二个活跃 run → `ERROR 1062` ✅ |
| **V3 并发实测（负面 + 修法）** | 两独立会话、RC、忠实复现生产语句：`SELECT MAX(seq) … FOR UPDATE` 双方都读到 `@m=0` → 后写者 `1062`；单语句 `INSERT … SELECT IFNULL(MAX(seq),0)+1` **同样 1062**；**修法**：先对锚点行取 X 锁（`SELECT thread_id FROM threads_meta WHERE thread_id=? FOR UPDATE`）→ B 阻塞约 3s → 读到 `max=1` → 插 `seq=2`，**两者都成功、序号连续** |
| **静默失效面（编译 + 执行双向探针）** | SQLAlchemy 2.0.49 编译：`UPDATE/INSERT/DELETE … RETURNING` **原样输出**（MySQL 侧 `1064`）；`date_trunc('minute',…)` **原样输出**（`1305`）；`EXTRACT(epoch FROM …)` **原样输出**（`1064`）；`ON CONFLICT DO NOTHING` **编译期 `UnsupportedCompilationError`（响亮失败）**；`FOR UPDATE SKIP LOCKED` 渲染正确；`.as_string()` 渲染为 `CASE JSON_EXTRACT(…) WHEN 'null' THEN NULL ELSE JSON_UNQUOTE(…) END`（方言中立） |
| **类型映射实测** | `DateTime(timezone=True)` → MySQL `DATETIME`（**`timezone=True` 被静默忽略**）；`DATETIME` 插入 `.123456` 后 `MICROSECOND()` 读回 **0**，`DATETIME(6)` 读回 **123456** ⇒ **亚秒静默截断**；`LargeBinary` → `BLOB`，100 KB 写入 → **`ERROR 1406 Data too long`**（`LONGBLOB` 正常） |
| **服务端能力实测** | `JSON_TYPE()` → `INTEGER`/`STRING`/`BOOLEAN`/`NULL`/`DOUBLE`/`OBJECT`/`ARRAY`（**大写**）；`CREATE INDEX … WHERE …` → `1064`（无 partial index）；`GET_LOCK`/`RELEASE_LOCK` → `1`/`1`；`INSERT IGNORE` 幂等 ✅；`@@transaction_isolation = REPEATABLE-READ`、`@@max_allowed_packet = 67108864`、`sql_mode` 含 `STRICT_TRANS_TABLES` |
| **auth 约束判别失效（源码 + 消息格式双向核对）** | `auth/repositories/sqlite.py:32-49` 读 `exc.orig.__cause__.constraint_name`（**asyncpg 属性**）；`:52-76` 回退要求消息含 `oauth_provider`；`:79-87` 要求含 `users.email`；`:90-98` 要求 `sqlstate` 或 `"unique constraint failed"`。而 MySQL 1062 消息实测为 `Duplicate entry 'github-oid-9' for key 'users.idx_users_oauth_identity'` / `'a1@x' for key 'users.ix_users_email'` ⇒ **三者全部返回 `False`** |
| **`engine.py` 的 DDL 面（逐处）** | `:58-82` `_auto_create_postgres_db`（连维护库 `CREATE DATABASE`）、`:199-212` `CreateSchema`、`:213` `bootstrap_schema(...)`、`:214-236` "does not exist" 时自动建库 + 重建 engine + 重跑；唯一生产调用点 `app/gateway/deps.py:432` |
| **`bootstrap_schema` 是执行器不是校验器** | `bootstrap.py:603-701`：`empty` → `create_all` + `stamp`（`:628-632`）；`legacy` → `_run_baseline_create_all_sync` + `stamp` + `upgrade`（`:634-653`）；`versioned` → `upgrade head`（`:655-693`）；否则 refuse（`:695`）。⇒ **生产路径不能复用它做校验** |
| **`MemoryThreadMetaStore` 的调用面** | 生产仅 `deps.py:496-498`（DB 模式下 `sf is not None` ⇒ 选中 `ThreadMetaRepository`）；实际消费者**全是测试** + `scripts/benchmark/checkpoint/bench_production.py:495,517`；只用到 `BaseStore` 的 **4 个方法**（`aget`/`aput`/`adelete`/`asearch`） |
| **`server_default` 审计** | 存活表中仅 **4 个**字段带 `server_default`（`scheduled_task_runs.attempt_count`、`runs.operation_kind`、`runs.token_usage_by_model`、`scheduled_tasks.last_occurrence_seq`），全部 PG 已有 |
| **绕过 ORM 的写入审计** | 全仓 raw `INSERT INTO` 仅在 `community/aio_sandbox/network_proxy.py`（自有 SQLite）与 `agents/task_continuity/archive.py` / `deermem/core/retrieval.py`（FTS5）—— **零处写应用 ORM 表** |
| **vendor 路径可行性验证** | 按 §7.4 附 B 的 6 处 import 改写**程序化构建探针包** → `applied 6 import rewrites`、`residual upstream imports: NONE`、`MIGRATIONS entries = 22`、渲染出的 22 条 SQL 在真实 8.0.24 上全部执行成功 ⇒ **vendor 路径可行，但默认不采用** |
| **基类接口** | `.venv/.../langgraph/checkpoint/base/__init__.py`：`delete_for_runs:331`、`copy_thread:350`、`prune:374`、`aget_tuple:429`、`alist:443`、`aput:468`、`aput_writes:491`、`adelete_thread:511`、`adelete_for_runs:522`、`get_next_version:692`、`get_serializable_checkpoint_metadata:778`、`WRITES_IDX_MAP:795`；**全部用 `raise NotImplementedError` 而非 `@abstractmethod`** |
| **checkpointer 生产调用点** | `services.py:1116,1136,1142,1347`、`threads.py:682,800,919`、`checkpoint_state.py:155,161`、`checkpoint_mode.py:140,146`、`worker.py:1657`、`client.py:681,735`；`copy_thread`/`prune`/`delete_for_runs` **零生产调用**（仅 `cached_saver.py:268-322` 透传） |
| LangGraph 后端能力 | `ls .venv/lib/python3.12/site-packages/langgraph/checkpoint/` → `base memory postgres serde sqlite`；`grep -rli mysql .venv/.../langgraph/`（零命中） |
| PG saver DDL/SQL | `.venv/.../langgraph/checkpoint/postgres/base.py:47-123,195-202` |
| **方言编译探针** | `/tmp/dialect_probe.py`：`UPDATE…RETURNING`、`with_for_update(skip_locked/read)`、`SELECT max(…) FOR UPDATE`、`JsonMatch`、`DATETIME` fsp、键长估算 |
| **`mcp_tasks` 无消费者证据** | `config.yaml:247-256`（`enabled: false`）；`config/mcp_tasks_config.py`（默认 `False`）；`mcp/tools.py:658`（`_make_background_submit_tool`）、`:745-763`（**只遍历 `server_config.task_toolsets`**）；**全仓 `task_toolsets` 的实际声明只在 `backend/tests/`**，`config.yaml` / `config.example.yaml` 零命中；`mcp/tasks/runtime.py:112-113`（配置了 toolset 但未启用即抛错） |
| **`subagent_batches` 无消费者证据** | `config.yaml:112-123`（`enabled: false`）；`config/subagent_batches_config.py:9`（默认 `False`）；`app.py:401-416`（只有 `enabled` 为真才 `start()` + `set_subagent_batch_submitter()` + `available = True`）；`routers/subagent_batches.py:86`（`available` 为假即拒绝）；`routers/features.py:57,64`（feature flag）；`factory.py:358-373`（batch 工具挂载条件） |
| 配置模型 | `config/database_config.py:142-209`、`config/checkpointer_config.py:9-32`、`config/object_storage_config.py`、`config/app_config.py:323-329,473-519` |
| PostgreSQL 适配层 | `persistence/postgres_schema.py`、`persistence/engine.py:30-50,58-92,117-131,169-233`、`persistence/bootstrap.py`（700 行） |
| ORM 模型 | `persistence/base.py` + 12 个 `*/model.py`（**12 张应用表 / 139 列**，逐表列数见 §2.2） |
| Migration（PG 链，**immutable、不被 MySQL 引用**） | `persistence/migrations/versions/0001_baseline.py` … `0023_user_preferences.py`（24 个文件）、`migrations/env.py:92-101`、`migrations/_helpers.py:31-128`、`migrations/_env_filters.py:28-35`、`migrations/AGENTS.md:118-142`（**含 `:128` 的"独立 alembic chain + 独立 `version_table`"先例**） |
| **Bootstrap** | `persistence/bootstrap.py`：三分支状态机、`_MIGRATIONS_DIR`（**单 `script_location`**）、`_HEAD_REVISION:109` / `_KNOWN_REVISIONS:110` **模块级缓存**、`_get_head_revision():331-340`、`_get_known_revisions():345-351`、`_CANONICAL_0019_SCHEMA_FLOOR:134-171`、`_BASELINE_TABLE_NAMES:198-210`、`_BASELINE_INDEX_NAMES:215-249`、`_PG_LOCK_KEY:183`、`_read_database_revision()`（要求**恰好一行**）、`bootstrap_schema(engine, *, backend, postgres_schema="")` |
| Checkpointer | `runtime/checkpointer/{provider,async_provider,cached_saver}.py`、`checkpoint_patches.py`、`runtime/checkpoint_mode.py:1-70`（`full` / `delta` 的定义） |
| **Store（删除对象）** | `runtime/store/{provider,async_provider,_sqlite_utils}.py`；唯一真实消费者 `persistence/thread_meta/memory.py:25-27`；构造点 `app/gateway/deps.py:435`（**无条件**）与 `:498`；5 处 `store=` 挂载点 |
| **同步 / 异步消费者** | `persistence/agents/sql.py:45-52`（**同步** `create_engine`）；`client.py:145,471-475,618-624,918-922`（`DeerFlowClient`，**零生产引用**）；`runtime/checkpointer/provider.py:103,115-143`（同步 Saver 分支） |
| 并发原语 | `runtime/events/store/db.py:110-190`、`persistence/run/sql.py:540-640`、`persistence/scheduled_task_runs/sql.py:195-220,300-330,750-770`、`persistence/scheduled_tasks/sql.py`、`persistence/user/preferences.py:4-25` |
| **JSON 方言 hack** | `persistence/json_compat.py`（231 行）：`_SQLITE:147-156`、`_PG:158-167`、`_build_clause:182-201`、`_compile_sqlite:204-212`、`_compile_pg:215-222`、**`_compile_default:225-227`（`raise NotImplementedError`）**、`json_match:230-231`；调用点 `thread_meta/sql.py:14,230,247,261` |
| 对象存储 | `object_storage/{port,keys,outputs,uploads,__init__}.py`、`config/object_storage_config.py` |
| 健康检查 | `app/gateway/health.py:71-260` |
| 多实例闸门 | `app/gateway/deps.py:49-113,388-391` |
| 部署 | `deploy/helm/deer-flow/values.yaml:119-160,185-216`、`docker/*.yaml`、`config.example.yaml:1794-1820,1877-1886` |
| 既有相关分析 | `docs/architecture/redis-checkpoint-store-feasibility.md`、`docs/architecture/phase5-*.md` |
| 测试 | **91 个**命中 `postgres` 的 `.py` 文件（`packages/harness/deerflow` 39 + `app/` 5 + `tests/` 47）；**7 个 PG 专属文件** |

### 合规声明

本次调查**未执行**以下任何动作：

- ❌ 未修改生产代码、数据库 Schema、migration 文件或依赖声明；
- ❌ 未创建任何 migration revision；
- ❌ 未替换任何数据库驱动；
- ❌ **未把 `langgraph-checkpoint-mysql` 安装进项目 venv** ——
  仅**下载 wheel 到 `/tmp` 并解包读源码**（`.whl` 与解包目录均在 `/tmp`，不进仓库）；
- ❌ **未把任何上游源码复制进仓库** —— §7.4 附 A 指定的落点
  `backend/packages/harness/deerflow/runtime/checkpointer/mysql/` **尚未创建**；
  且按默认路径（精确版本直接依赖），**它也不应该被创建**，
  除非 §7.3 第 4️⃣ 档被触发；
- ❌ 未新增任何依赖（`harness/pyproject.toml` 的 `mysql` extra 只是方案）；
- ❌ 未执行任何生产迁移或数据变更。

**✅ 唯一"动了环境"的部分（已清理，不影响仓库）**：

- 为完成 V3 / V6 / OAuth / 迁移链的**实证**，起了一个**一次性 Docker 容器**
  `mysql:8.0.24`（端口 13306，库名 `dfverify` / `ckpt_probe`），
  只用于跑探针 SQL 与执行上游 22 条 migration。
  **容器与其中的数据均为临时验证用途，不构成任何部署形态**，
  也不代表本次迁移引入了容器化部署（K8s / Helm 仍不在范围内，§3.4）。

全部结论均来自**只读**的代码阅读、`.venv` 包目录树检查、
第三方包 wheel 的源码阅读、ORM 元数据反射、方言编译探针，
以及**真实 MySQL 8.0.24 上的执行探针**
（脚本、wheel、渲染出的 SQL 均置于 `/tmp`，不进仓库）。


