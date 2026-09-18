# PostgreSQL → MySQL 8.0.24 迁移可行性分析（可执行迁移设计 · 第五轮修订）

> 🔴 **【已取代 · SUPERSEDED · 仅作过程留档，不要再据此实施】**
>
> 本文是多轮迭代的**过程稿**，基线停在 **`dba975ef`**（G0 之前），
> 其中的 CheckpointSaver 选型（"候选 / 待定 / 可能 vendor"）、量化口径（20 表 / 274 列 / 15 表 / 224 列）
> 与锁锚点表名（`threads`）**均已过时**。
>
> **权威口径一律以 [`mysql-migration-design.md`](./mysql-migration-design.md) 为准**
> （基线 `a55e5734`，G0 完成；CheckpointSaver 已冻结为 `langgraph-checkpoint-mysql[asyncmy]==3.0.0`；
> 12 表 / 139 列；锁锚点为 `threads_meta.thread_id`）。
> 多轮修订的对照记录见 [`mysql-migration-plan.md`](./mysql-migration-plan.md)；
> 执行计划见 [`mysql-goals.md`](./mysql-goals.md)，G0 审计见 [`mysql-goal0-audit.md`](./mysql-goal0-audit.md)。

> **文档类型**：只读代码调查 + 依赖分析 + Schema 分析 + 迁移设计
> **调查基线**：`deer-flow` 仓库 HEAD = **`dba975ef`**（`feat_portal` 分支，含 Phase 5 Goals 1–3 与生产闸门）
> **首次调查基线**：`30f45d63`（Phase 5 Goals 1–2）；第二～四轮修订均在同一 HEAD 上完成
> **上一版基线**：`cfd41bf6`（本文档第一版，2026-09-16）
> **本版日期**：2026-09-17
> **目标数据库**：**MySQL 8.0.24** + InnoDB
> **本次修订（第五轮，同日）：迁移范围瘦身 —— 先删除不需要的能力，再迁移必须保留的能力** ——
> 本轮**不做**任何 Schema 规范细化或实现展开，只做**范围收缩**。
> 七项收缩（逐项见 §0 第五轮表，最终口径见 **§1.7**）：
> ① 🔴 **删除 LangGraph Store 迁移范围**：不再实现 MySQL Store、**不创建 `ag_store`**、
>    不做第三方 MySQL Store 评估、不做 Store health/config/provider 迁移、
>    不做 pgvector / Store vector 相关讨论（§9）；
> ② **删除全部历史数据库兼容设计**：`0024` / PG `0001`–`0023` replay /
>    Existing Instance upgrade / backfill migration / PG→MySQL 数据转换 /
>    dual-read / dual-write / 数据层回滚到 PG（第四轮已收敛，本轮**彻底清除残留**）；
> ③ **删除 Channel 相关 MySQL 工作**，并在当前 HEAD 上**重新统计**全部口径（§1.7）；
> ④ **暂不引入 Redis checkpoint cache 优化**：保持 `checkpoint_channel_mode = full`，
>    不切 `delta`，不为未来的 namespace / invalidation / cascading eviction 提前设计；
> ⑤ **重新评估同步 Checkpointer 路径**：生产主路径是 Gateway async runtime ⇒
>    第一阶段**只实现 async CheckpointSaver**（§8.6）；
> ⑥ **不做无关的索引优化**：冗余索引清理 / query tuning / 索引合并 / 推测 workload 加索引
>    全部留到 MySQL 实际运行后按 `EXPLAIN` 单独处理（§15.7）；
> ⑦ **bootstrap 最小化**：不实现分布式数据库锁（**单一 Migrator**），
>    不把 PG 的 schema-floor / baseline-pin 逻辑迁入 MySQL（§7.8.5）。
> **档位不变**：仍为 `Feasible with significant changes` ——
> 本轮删掉的是**范围**（Store / 历史兼容 / 渠道 / 同步 Saver / 索引优化），
> 而 **CheckpointSaver 自研**与**并发语义替换**两项代价**丝毫未变**（§21.2 / §21.3）。
> **本次修订（第四轮，同日）：定性收敛为「fresh-cutover」，MySQL 走独立 migration chain** ——
> 补充确认：**不迁移任何 PostgreSQL 历史数据**、**MySQL 上线后 PostgreSQL backend 直接废弃**、
> **不支持 PostgreSQL 原地升级到 MySQL**、**无 dual-read / dual-write / backfill**。
> 因此本次属于 **MySQL fresh-cutover / PostgreSQL backend replacement**，**不是历史数据迁移**。
> 由此产生 6 项设计收敛（逐项见 §0 的第四轮表）：
> ① 🔴 **取消 `0024_mysql_baseline` 方案** —— 不再把 MySQL baseline 接到 PG `0001`–`0023` 之后；
> ② **MySQL 使用独立 migration chain**，起点为 **`0001_mysql_baseline`**，
>    MySQL Fresh Instance **不执行** PG `0001`–`0023`；PG 历史 migration 保持 **immutable**，
>    暂时只作为**旧实现历史**存在（§7.8）；
> ③ **`0001_mysql_baseline` 直接体现裁剪后的最终 Schema** ——
>    已决定删除的 Channels / GitHub Webhook 及其表、索引、约束**不进入** MySQL baseline，
>    因此**不再需要"先建后 drop"的 drop migration**（§1.5）；
> ④ **重新评估 bootstrap 逻辑**：`_CANONICAL_0019_SCHEMA_FLOOR` /
>    `_BASELINE_TABLE_NAMES` / `_BASELINE_INDEX_NAMES` / `_FORWARD_COMPATIBLE_REVISION` /
>    `_validate_forward_schema` / `_run_baseline_create_all_sync` 等**只服务旧 PostgreSQL bootstrap**，
>    一律标记为 **legacy**，**不带入 MySQL**，随 PostgreSQL backend 一并删除（§7.8）；
> ⑤ **重定义 MySQL migration 验收**：空 MySQL 8.0.24 → `0001_mysql_baseline` →
>    顺序执行 MySQL 后续 revision → **只有一个 MySQL head**，
>    且 **fresh bootstrap 不依赖任何 PostgreSQL migration 或 PostgreSQL-specific SQL**（§7.8）；
> ⑥ **同步重算迁移范围与风险清单**（§1.6 / §17 / §21.3）——
>    已随 Channel 一起消失的问题**不再出现于清单**，也不再为待删除代码设计 MySQL 兼容方案。
> → **V1 因此大幅收窄**：原"**multiple heads** / 重放不适用的 PG 历史 revision"这一风险
>    **随独立链消失**，剩下的是独立链的**接线方式**（§20.11）。
> **本轮结论档位仍不变**：`Feasible with significant changes`。
> **本次修订（第三轮，同日）**：按两项**架构决策**再次缩减范围 ——
> ① 🔴 **Channel 范围沿用 `feature-inventory` 的既有决策：整体删除。**
> 删除 DeerFlow 当前 Channels、GitHub Webhook 及其现有渠道数据模型
> （`channel_connections` / `channel_conversations` / `channel_credentials` /
> `channel_oauth_states` / `webhook_deliveries` 共 5 张表），
> **本次 MySQL 迁移不再为这些待删除能力设计任何兼容方案**。
> 未来如需 Custom Channel，基于**统一事件入口**重新设计，**不要求兼容当前 Channel Core**。
> → 改造范围由 **20 表 / 274 列** 缩减为 **15 表 / 224 列**，
> 并**消除**了原唯一的确定性硬阻塞（`webhook_deliveries` 8448 字节主键）与
> 最高难度项（条件 upsert 重构）。**范围差异见 §1.5，缩减后的量化对照见 §1.6。**
> ② **Sandbox ownership 的 Redis 持久化方案本阶段不决策**，留到后续执行环境改造阶段；
> 当前只确定一条约束：**sandbox ownership 属于 lease / correctness state，
> 不得放入"重启即空"的 volatile Redis**（§20.13）。
> **V1–V4 均属工程调查 / 实测项，不再需要业务确认** ——
> 由实现前验证后**直接给出技术结论**（§20.11）。
> **本轮结论档位不变**：仍为 `Feasible with significant changes`。
> ⚠️ 但要说清缩减的边界：**缩减的是"范围"，不是"性质"** ——
> 三大风险里只有 Schema 硬约束一项被削弱，
> **CheckpointSaver 自研**与**并发语义替换**两项**丝毫未变**（见 §21.2 / §21.3）。
> **上一轮修订（第二轮）**：`run_events.seq` 由 Easy 上调为 Moderate 并新增 V3；
> Redis checkpoint cache 不与迁移同时切换（R1）；新增 V4 / R2 / R3 / R4；
> V1/V2 升格为实施前 Gate。
> **第一轮**：§4.2 表业务语义总览；§18.1–§18.7 Redis 接入评估；
> §20 的 12 项 Open Questions 逐项收敛为设计决策；§19 重排为 6 阶段。
> **合规声明**：本任务**未**修改任何生产代码、Schema、migration、Docker 初始化文件、
> 数据库驱动，**未**创建 migration，**未**新增数据库实现或测试，**未**执行任何数据迁移。
> 所有结论来自只读代码检索、依赖清单核对、**本机 venv 实测版本**、**SQLAlchemy 方言编译探针**
> （脚本置于 `/tmp`，未写入仓库），以及**从 PyPI 下载到 `/tmp` 解包阅读**的第三方源码。

---

## 0. 本版相对第一版的修正与新发现

第一版（基线 `cfd41bf6`）的核心判断仍然成立，但**有 6 处事实错误或遗漏**，
以及 4 项因 HEAD 前移而产生的新事实。全部按"以实际代码为准"重写：

| # | 第一版结论 | 本版实测结论 | 影响 |
| --- | --- | --- | --- |
| 1 | 本地 venv **未安装** `langgraph-checkpoint-postgres`（"postgres extra 未 sync"） | **已安装 3.1.1**（`importlib.metadata` 实测） | 消除了"该路径未被真实验证"的疑点；但 saver 的 DDL 也因此可被逐行核验 |
| 2 | `sa.JSON` 列 **24** 个 | **25** 个 | 改造清单遗漏 1 列（`user_preferences.value` 已计入，实际漏的是 `managed_subagents.definition` 的计数口径） |
| 3 | Partial unique index **2** 处 | **4** 处 | 生成列 workaround 的工作量翻倍，且多出两个**跨进程唯一性保证**（渠道所有权、调度 occurrence） |
| 4 | `RETURNING` **2** 处 | **5** 处（4 处 ORM `.returning()` + 1 处裸 SQL） | 第一版漏了 `persistence/run/sql.py:563,594,627` 三处**运行所有权核心路径** |
| 5 | 命中 `postgres` 的测试文件 **48** 个 | **111** 个 | 测试面改造工作量约为第一版估计的 2.3 倍 |
| 6 | `with_for_update` 未系统盘点 | **51 处 / 8 个文件** | MySQL 下"无索引即锁全表"的风险面需要逐条核验 |
| **7** | 未识别 | **`webhook_deliveries` 主键 8448 字节 > InnoDB 上限 3072 字节** | **新增硬阻塞**：该表按现状**无法在 MySQL 上建表** |
| **8** | 未识别 | **SQLAlchemy 2.0.49 会把 `UPDATE … RETURNING` 编译成 MySQL 非法 SQL 且不报错** | **新增陷阱**：编译期/单测期静默通过，运行期才 1064 |
| **9** | 未识别 | `with_for_update(read=True)` 在 MySQL 方言下编译为 **`LOCK IN SHARE MODE`** | `mcp_tasks/sql.py:166-169` 的 PG 四档行锁论证在 MySQL 上不成立 |
| **10** | 未识别 | `SELECT max(…) FOR UPDATE` 在 MySQL **编译正常** | `runtime/events/store/db.py:146-153` 的 advisory-lock 分支**不能**据此直接删除 —— **本版已修正**（见下条与 §13.7） |
| **11** | Object Storage 只有 `.tool-results` 雏形 | **HEAD 已落地 S3/MinIO 对象存储端口**（`object_storage/`，aiobotocore 流式 multipart） | 方案 C（External Storage）从"要新建基础设施"变为"**已有端口可直接复用**" |
| **12** | 未识别 | **`0018_oauth_identity_pg_partial` 读 `pg_index` / `to_regclass` 系统目录** | 破坏"Fresh Instance：原始脚本 → migration 001 → …"的可重放性 |
| **13** | 本版**自身**的过度推断 | `runtime/events/store/db.py:133-153` 的 `else` 分支**不是"通用实现"，而是"SQLite 语义"**：SQLite 忽略 `FOR UPDATE`，其正确性来自"单写者库锁"，与 `FOR UPDATE` 无关。MySQL 会**静默落入同一分支** | 🔴 **本版已自我修正**：不能因为"MySQL 编译通过"就认定它等价于 PG 的 advisory lock。该处难度由 **Easy 上调为 Moderate**，并新增 **V3**（§13.7） |
| **14** | 本版**自身**的过度推断 | StreamBridge 的 gap 检测**只覆盖"保留窗口被 maxlen/TTL 裁掉"**，**不覆盖 Redis 重启丢流**；且 SSE 路径**没有** RunEventStore 自动补偿 | ⚠️ 不能声称"`run_events` 会自动回源"。新增 **V4**（§18.1） |
| **15** | 未识别 | `run_events` 的 `put` / `put_batch` 在 seq 冲突时**没有任何重试**（`runtime/events/` 全目录无 `IntegrityError` 处理） | 一次分配竞态 = 整个 batch 事务失败，不是"重试一次就好" |

**本次修订（第二轮）另外修正了 3 项跨章节的一致性**：

1. **`SELECT max(…) FOR UPDATE` 的结论在 3 处出现，全部同步**（§0 #10、§10.2 #15、§21.2 支撑证据表）——
   这正是"同一结论散落多处"的典型风险，只改一处会留下自相矛盾的文档。
2. **Redis checkpoint cache 的启用时机**：从"立刻做"改为"**MySQL Saver 稳定后单独评估**"（§18.3）。
   迁移期**保持 `checkpoint_channel_mode = full`**，不为利用缓存而同时改变 checkpoint 存储语义。
3. **Redis Stack 的部署结论**：从"基线定为 vanilla Redis 7"放宽为
   "**可以用 Redis Stack 7 镜像部署，但架构不得依赖任何 Stack module**"（§18.6）。

**本次修订（第三轮）：范围缩减 —— Channel 整体删除**

| # | 上一版的判断 | 本版修正 | 影响 |
| --- | --- | --- | --- |
| **16** | §20.8 决策为"**Channel 核心保留**，只重构 5 张表的职责" | 🔴 **改为整体删除**：`feature-inventory` 的既有决策（Rev.3 / Q2 / Q19 / C7 / C14 / E8）本就是**删除** Channels + GitHub Webhook + 其数据模型；`app/channels/AGENTS.md` 的"tables retained for future extensions"是**与产品决策不一致的局部表述**，以 `feature-inventory` 为准 | **20 表 → 15 表、274 列 → 224 列**；详见 §1.5 / §1.6 |
| **17** | `webhook_deliveries` 主键 8448 字节是"**唯一确定性硬阻塞**" | ✅ **该阻塞消失**（表被删除）。原 **B1** 从阻塞清单移除 | §1.1 代价三、§15.6、§17.2、§21.3 |
| **18** | `dedupe_store.py` 的条件 upsert 重构被标为 **High（最高）难度** | ✅ **该难度项消失**（`app/channels/dedupe_store.py` 随渠道删除）。`RETURNING` 由 5 处降为 **4 处**、`ON CONFLICT` 由 2 处降为 **1 处**、`make_interval` 由 2 处降为 **0 处** | §10.2 #5、§13.4 |
| **19** | 3 处事务级 advisory lock | 降为 **2 处**（`channel_connections/sql.py:398` 随渠道删除） | §1.1 代价二、§10.2 #1 |
| **20** | 4 处 partial unique index / 4 处 FK | 各降为 **3 处 / 2 处**（`uq_channel_connection_active_identity` 与 2 个渠道 FK 随表删除） | §3.5、§4.5 |
| **21** | Sandbox ownership 与 volatile Redis 前提冲突 | **本阶段不决策**持久化方案；只固定约束"属 lease / correctness state，**不得**放入重启即空的 volatile Redis" | 新增 §20.13 |

> ⚠️ **一处必须避免的命名混淆**：`test_delta_channel_*.py`、`test_bench_checkpoint_channels.py`、
> `test_summarize_checkpoint_channels.py` 里的 "channel" 指 **LangGraph 的 `DeltaChannel`
> （checkpoint 增量通道）**，**与 IM 渠道无关**，**不得**随渠道删除。详见 §19 阶段 0。

**本次修订（第四轮）：定性收敛为 fresh-cutover + MySQL 独立 migration chain**

| # | 上一版的判断 | 本版修正 | 影响 |
| --- | --- | --- | --- |
| **22** | MySQL Fresh Baseline 命名为 `0024_mysql_baseline`，**接在 PG `0001`–`0023` 之后** | 🔴 **取消 `0024` 方案**。不再把 MySQL baseline 挂到现有 PG revision tree 后面 | 编号从 `0024` 改为 **`0001_mysql_baseline`**；原"`0024` 与现有 revision graph 的关系"讨论（§7.7、§20.2、§20.11 V1）**整体改写** |
| **23** | 隐含"MySQL 与 PG 共用一条 Alembic 链" | **MySQL 使用独立 migration chain**：独立 `script_location` + 独立 `version_table`。MySQL Fresh Instance **不执行** PG `0001`–`0023` | 新增 §7.8；`migrations/AGENTS.md:128` 已有"独立链 + 独立 `version_table`"先例，**无需新机制** |
| **24** | 渠道表"先由 baseline 建出，再由 drop migration 删除"（因为要兼容 PG 历史） | ✅ **改为"根本不进入 MySQL baseline"**：`0001_mysql_baseline` 直接体现裁剪后的 15 张表 | **drop migration 需求消失**；`bootstrap.py` 三个常量的"反向 pin 冲突"**随之消失**（§7.2、§7.8） |
| **25** | `_CANONICAL_0019_SCHEMA_FLOOR` / `_BASELINE_TABLE_NAMES` / `_BASELINE_INDEX_NAMES` 是"必须适配的难点" | 🔴 **改为标记 legacy**：它们只服务**旧 PostgreSQL bootstrap**（`create_all` 基线 + `0019` 前向兼容 + 反向 pin 测试），**不带入 MySQL**，随 PG backend 一并删除 | §7.8 给出逐项 legacy 清单与删除时点；原"本阶段唯一的真实设计难点"表述作废 |
| **26** | 验收标准含"fresh install 起点为 `0024`"、"`alembic heads` 只有一个 head" | **重定义为**：空 MySQL 8.0.24 → `0001_mysql_baseline` → 顺序执行 MySQL 后续 revision → **只有一个 MySQL head**；**fresh bootstrap 不依赖任何 PG migration 或 PG-specific SQL** | §7.8 验收清单；§20.11 V1 改写 |
| **27** | `0018_oauth_identity_pg_partial` 读 `pg_index` → 构成"**需要用户确认的第一号规范偏差**" | ✅ **该偏差由构造消除**：MySQL 从不重放 `0018`，因此**不再需要用户确认**。原"规范冲突 1"改写为"**已由独立链解决**" | §7.7；仅剩"migration 载体是 Alembic Python revision 而非裸 SQL"这一项偏差 |

> 🔴 **一处必须说清的逻辑关系**：`0024` 的取消**不是**把 PG 历史链"往后接一节"，
> 而是**彻底不共用链**。这两者在实现上完全不同 ——
> 前者需要处理 multiple heads 与 PG 专属 revision 的重放，后者**天然不存在这两个问题**。
> 因此本轮 V1 的**验证面缩小**，但**结论的性质没有变**：
> MySQL 侧的 Schema / CheckpointSaver / 并发语义改造量与第三轮完全一致。

**本次修订（第五轮）：迁移范围瘦身 —— 先删除不需要的能力，再迁移必须保留的能力**

| # | 上一版的范围 | 本版修正 | 影响 |
| --- | --- | --- | --- |
| **28** | 需**自研 MySQL Store** + 创建 `ag_store` | 🔴 **删除**：不实现 MySQL Store、不创建 `ag_store`、不做第三方 Store 评估、不做 Store health/config/provider 迁移、不做 pgvector 讨论。**但必须连带处理 3 处耦合**（`_sqlite_utils` 被 checkpointer/health 复用、`MemoryThreadMetaStore`、`deps.py:435` 的无条件构造 + 5 处 `store=` 管线 + `app.py` 孤儿迁移） | §9 整体改写；框架表 6 → 5；§7.2 映射 21 → 20 张 |
| **29** | 残留的"历史数据迁移"语境（backfill / 数据转换 / 数据层回滚 / Existing Instance upgrade） | 🔴 **彻底清除**：本轮是 fresh-cutover，**没有** backfill migration、**没有** PG→MySQL 数据转换、**没有**数据层 rollback 到 PG | §1.7 / §19 / §20.6 |
| **30** | Channel 相关 MySQL 工作仍在文档中（OAuth lock / inbound dedupe SQL / partial unique / 8448 字节主键 / FK cascade / Redis dedupe 方案） | 🔴 **全部删除**，并在当前 HEAD 上**重新统计**全部口径 | §1.7 给出权威重算表 |
| **31** | Redis checkpoint cache 作为"迁移后可评估项"反复出现 | **明确移出本阶段**：保持 `checkpoint_channel_mode = full`，不切 `delta`，不启用新 cache，**不为未来 cache 提前设计 namespace / invalidation / cascading eviction** | §18.3 / §19 阶段 7 |
| **32** | 同步 Checkpointer / Saver 也在迁移范围内 | 🔴 **第一阶段只实现 async CheckpointSaver**。同步 Saver 的**唯一生产消费者是 `DeerFlowClient`**（已降级为调试），TUI 已删 ⇒ 不为它实现同步 MySQL Saver。⚠️ **但同步数据库驱动必须保留** —— `agent_storage.backend: db` 走同步 SQLAlchemy（`SqlAgentStore`），这是**独立的同步驱动刚需** | §8.6 新增；§20.3 驱动结论修正 |
| **33** | 索引分析里含"新增/删除冗余索引"的工作量 | **不做无关索引优化**：冗余索引清理 / query tuning / 索引合并 / 推测 workload 加索引全部**留到运行后按 `EXPLAIN` 单独处理** | §15.7 新增 |
| **34** | bootstrap 需要"会话级 advisory lock"（MySQL 侧二选一） | 🔴 **直接不做 DB 锁**：所有 MySQL 实例都是空库 ⇒ 采用**单一 Migrator 执行 migration 后再启动多实例**。`_postgres_lock` 随 PG backend 退役，**不迁入 MySQL** | §7.8.5 定稿；原 V1 子问题关闭 |
| **35** | `_CANONICAL_0019_SCHEMA_FLOOR` / `_BASELINE_TABLE_NAMES` / `_BASELINE_INDEX_NAMES` 等"标记 legacy 待删" | **再确认并保持**：只服务旧 PG migration history ⇒ **不迁入 MySQL bootstrap**（第四轮已定，本轮复核） | §7.8.4 |
| **36** | 未识别 | 🔴 **新增发现（本轮调查）**：§9.2 原文称"Store 承载 `agents` 命名空间的 agent 定义" —— **该表述错误**。`setup_agent_tool` 用的是 `deerflow.persistence.agents.get_agent_store()`（**自研 AgentStore**，file/db），与 `BaseStore` 无关；Store 的真实内容是 **memory 模式下的 `("threads",)` 线程元数据**，DB 模式下**只被构造、不被读写** | §9.2 已更正；结论不变（**更支持删除**） |

> 🔴 **本轮必须说清的一条**：瘦身删掉的是"**要迁移多少东西**"，
> **不是**"迁移有多难"。删掉 Store 不会让 checkpoint saver 变简单，
> 删掉同步 Saver 也不会让并发语义替换变简单 —— 这两项**仍是全部难度的来源**。

---

## 1. Executive Summary

**整体结论：`Feasible with significant changes`。**

当前项目**可以**移除 PostgreSQL 并落到 MySQL 8.0.24，且**可以在严格满足本任务
§1–§7 全部 MySQL 规范的前提下完成**——但代价集中在三处，任何一处不成立，
结论就应退化。

### 1.1 三处代价（按决定性排序）

**代价一：LangGraph 持久化层必须自研，且必须重新设计 blob 的物理载体。**

- 实测：`.venv/.../langgraph/checkpoint/` 只有 `base`、`memory`、`postgres`、`serde`、`sqlite`；
  `.venv/.../langgraph/store/` 只有 `base`、`memory`、`postgres`、`sqlite`；
  `grep -rli mysql .venv/.../langgraph/` **零命中**。
  → **LangGraph 官方没有 MySQL checkpointer，也没有 MySQL store。**
- 现役 `langgraph-checkpoint-postgres` **3.1.1** 的 DDL 硬编码 `checkpoint JSONB`、
  `metadata JSONB`、`blob BYTEA`、`blob BYTEA NOT NULL`、`CREATE INDEX CONCURRENTLY`；
  SQL 里硬编码 `jsonb_each_text`、`array_agg`、`::bytea`、`ANY(%s)`、`%s` 占位符。
  → 这不是"换驱动 + 换方言"能解决的组件。
- 第三方 `langgraph-checkpoint-mysql` **3.0.0**（PyPI 实测，作者 Theodore Ni，
  **非 LangChain 官方**）虽然能跑，但 DDL 用 `JSON` + `LONGBLOB` + `BINARY(16)`，
  表名无 `ag_` 前缀、零中文 COMMENT、索引命名不符、SQL 大量依赖
  `json_table`/`json_keys`/`json_extract`。
  → 判定为 **"需要定制 / 不兼容当前 Schema 规范"**，且因为 SQL 与 JSON 函数深度耦合，
  fork 的性价比低于自研。

**代价二：并发原语替换，且是"能跑但可能静默错误"的一类。**

实测的非测试生产代码使用面（**已按第三轮的 Channel 删除缩减**，括号内为缩减前的数值）：

| 原语 | 处数 | 位置 |
| --- | --- | --- |
| 事务级 advisory lock（`pg_advisory_xact_lock`） | **2**（原 3） | `runtime/events/store/db.py:148`、`persistence/scheduled_task_runs/sql.py:317`<br>~~`persistence/channel_connections/sql.py:398`~~ ← **随渠道删除** |
| 会话级 advisory lock（`pg_advisory_lock` / `_unlock`）+ `SET LOCAL idle_in_transaction_session_timeout` | 1 组 | `persistence/bootstrap.py:552,553,559` |
| `RETURNING` | **4**（原 5） | `persistence/run/sql.py:563,594,627`、`persistence/scheduled_task_runs/sql.py:211`<br>~~`app/channels/dedupe_store.py:153`~~ ← **随渠道删除** |
| `ON CONFLICT` | **1**（原 2） | `persistence/user/preferences.py:21-25`（`pg_insert`）<br>~~`app/channels/dedupe_store.py:150`~~ ← **随渠道删除** |
| Partial unique index | **3**（原 4） | 见 §3.5 |
| `with_for_update(...)` | **51** | 8 个文件，分布见 §13.2（**渠道表无 `FOR UPDATE`，故不缩减**） |
| `make_interval(secs => …)` | **0**（原 2） | ~~`dedupe_store.py:152,175`~~ ← **随渠道删除，该改写工作整体消失** |

MySQL **没有** `RETURNING`、**没有** partial index、**没有**事务级 advisory lock，
且 InnoDB 默认隔离级别是 **REPEATABLE READ**（PG 是 READ COMMITTED）。

> ⚠️ **"在目标库上能编译 / 能执行" ≠ "与原语义等价"。**
> 上表 2 处 advisory lock 中，`runtime/events/store/db.py:148` 那一处
> **不能**因为 `SELECT max(seq) … FOR UPDATE` 在 MySQL 上编译通过就认为可以直接替换 ——
> 该 `else` 分支原本是为 **SQLite** 写的（SQLite 忽略 `FOR UPDATE`，靠库级单写者锁保证正确性），
> MySQL 只是**静默落进了同一个分支**。详见 **§13.7** 与待实测项 **V3**。
> 同理，4 处 `RETURNING` 在 SQLAlchemy 下**编译期不报错**（§10.2 #17）。
> 这两件事是同一类错误：**编译通过是假阴性，不是证据。**

更麻烦的是：代码里存在**显式声明为 PostgreSQL 专属**的正确性路径 ——
`scheduled_task_runs/sql.py:758` 的注释原文是
**"Multi-instance reconciliation is Postgres-only."**。
这意味着 MySQL 不是"把后端换掉"，而是要为跨实例对账**新增第三条语义路径**
（PG / SQLite / MySQL 三套并存），而这条路径此前从未被定义过。

**代价三：Schema 层的硬约束（第三轮后，原本唯一的"建表就失败"已消除）。**

- ✅ **原唯一的确定性硬阻塞已消失**：`webhook_deliveries` 主键
  `(channel, workspace_id, chat_id, message_id)` = **8448 字节（utf8mb4）> InnoDB 3072 上限** ——
  该表**随渠道整体删除**（第三轮决策，§1.5），因此**不再需要**任何主键重设计。
  → 缩减后**没有任何一张表按现状建不出来**。
- ⚠️ InnoDB 索引键长上限 3072 字节（utf8mb4 下单列 ≤ 768 字符）。**剩余 15 张表**中，
  最大唯一键 1788 字节（`mcp_tasks.uq_mcp_tasks_user_server_remote`），安全。
- ⚠️ `DateTime(timezone=True)` 在 MySQL 方言下编译为 **`DATETIME`**（无 `fsp`）→
  **亚秒被截断**、**时区被静默丢弃**。剩余 **47** 个时间列受影响（原 61）
  （**已决策**：统一 UTC `DATETIME(6)`，§20.10 #1）。
- ⚠️ `TEXT` 上限 65,535 字节，而 checkpoint 的 `messages` 通道 blob 是累积 reducer。

### 1.2 分职责域结论

| 职责域 | 迁移难度 | 关键阻碍 | 是否必须动 PostgreSQL 之外的设施 |
| --- | --- | --- | --- |
| **Application Data**（**15 张表 / 224 列**） | **Moderate**（**↓ 比第三轮前更轻**） | **20** 个 JSON 列、**2** 处 FK、**3** 处 partial unique index、**48** 个时间列精度、2 张表的 JSON key 过滤需拆列<br>~~25 JSON / 4 FK / 4 partial unique / 61 时间列 / `webhook_deliveries` 主键超限~~ ← **全部随渠道删除而缩减** | 否（**已无任何表需改主键**） |
| **LangGraph Checkpoint** | **Hard** | `JSONB`/`BYTEA`/msgpack-pickle、`jsonb_each_text`、`%s`、`CONCURRENTLY`、官方无实现 | **是**：大 blob 需对象存储 |
| **LangGraph Store** | **Moderate** | `value JSON NOT NULL`、`TIMESTAMP`；但使用面极窄 | 否 |
| **Queue / Scheduler** | **Hard** | **2** 处 advisory lock（原 3）、**4** 处 `RETURNING`（原 5）、**3** 处 partial unique（原 4）、51 处 `FOR UPDATE`、RC→RR<br>**Inbound dedupe 子系统整体移出范围**（随渠道删除） | 否 |
| **Knowledge / Vector** | **Replace（不进 MySQL）** | 现状**未使用 pgvector**；RAG 走外部 RAGFlow HTTP；本地检索走 SQLite FTS5 | 否（保持现状） |

> **Queue / Scheduler 的难度仍为 Hard，但阻碍的"面积"缩小了**：
> 五套子系统变为**四套**（Scheduler / MCP Tasks / Subagent Batches / Run ownership），
> Inbound dedupe 不再需要迁移 —— 而它原本是**唯一被标为 High（最高）难度**的一项。

### 1.3 结论退化条件

以下任一条成立，结论应退到 **`Not currently recommended`**：

1. 组织**不能接受**自研并长期维护一个 MySQL `BaseCheckpointSaver`（含 `setup()` 迁移链、
   DeltaChannel 两阶段查询、并发写语义）。
2. 组织**不能接受**引入对象存储来承载 checkpoint 大 blob，**且**不接受把
   checkpoint payload 列放宽到 `MEDIUMTEXT`
   （注意：`LONGTEXT` 已明确**不采用**，见 §20.5 / §20.10）。
3. 组织要求 **MySQL 必须复用 PostgreSQL 的 Alembic revision 编号体系**
   （即**不允许**为 MySQL 提供独立 migration chain）。
   —— ⚠️ **第四轮修正措辞**：上一版写作"不允许为 MySQL 提供独立的 baseline 起点"，
   但第四轮**采用的就是独立链 + 独立 baseline 起点**（§7.8），因此该措辞已不成立。
   真正的退化条件是**禁止独立链**：那样就得回到 `0024` 方案，
   重新面对 **multiple heads** 与 `0018_oauth_identity_pg_partial.py` 读 `pg_index` 系统目录
   （§7.7 / §7.8.1）。

> ✅ **第三轮后，退化条件没有新增，反而少了一条隐含前提**：
> 原先"必须重新设计 `webhook_deliveries` 主键，否则该表建不出来"这条**已随渠道删除消失**。
>
> ⚠️ **第四轮补充**：条件 3 的措辞被**反向修正**了 ——
> 独立链不是"需要被容忍的偏差"，而是**本方案的组成部分**。
> 这一点很关键：如果误把独立链当成偏差，就会得出"应当避免它"的错误结论。

### 1.4 一个反直觉的结论（重要）

**"移除 PostgreSQL" 与 "降低迁移风险" 在当前代码结构下方向相反。**

`database.backend` 这一个字段同时驱动 **应用 ORM** 与 **LangGraph Checkpointer/Store**
（`runtime/store/provider.py:62-75` 的 `_resolve_store_config` 由 `database.backend` 派生
`CheckpointerConfig`）。但代码里**已经存在**一个独立的 `checkpointer:` 配置节
（`config/checkpointer_config.py:12-32`、`app_config.py:323-329`），
当它被显式设置时**优先于** `database`（`runtime/store/provider.py:83-85`）。

→ **配置模型已经支持"两条职责链用不同后端"**，只是 `CheckpointerType` 的
`Literal["memory","sqlite","postgres"]` 尚未包含新成员。
这意味着**分阶段迁移在配置层面是可表达的**，不必先做一次大爆炸式切换。
这是本版最重要的正向发现。

### 1.5 Channel 范围决策：**整体删除，不做兼容**（第三轮）

**决策来源**：`docs/architecture/feature-inventory.md` 的既有决策
（Rev.3 / Rev.3.1），不是本次新提出的。

| 决策点 | `feature-inventory` 原文位置 | 内容 |
| --- | --- | --- |
| **Q2** | §17.1 决策总览 | 保留定时任务 + 通用事件触发；**不需要 GitHub Webhook** → 明确删除 |
| **Q19** | §17.2 | channels **没有真实用户** → 由"先禁用 → 观察 → 再删除"改为**直接删除**，取消观察期 |
| **C7** | §12 / §15 | **8 个 IM 渠道**：失去 IM 入口 → **解耦后删除** |
| **C14** | §12 | **GitHub Webhook 全套** → 明确删除（与渠道**双向耦合**，必须同阶段） |
| **E8** | §13.6 | `persistence/webhook_delivery` **不是 GitHub 专用**，而是 `dedupe_store.py` 的多 Pod 入站去重表 → **与渠道强绑定，随渠道一并处理** |
| **D18** | §13 | 现状只有 GitHub Webhook，而决策**不需要** → **需新建通用事件入口**（Phase 7 净新增） |

**本次架构决策（明确化）**：

> 1. **删除 DeerFlow 当前 Channels、GitHub Webhook 及其现有渠道数据模型**；
>    本次 MySQL 迁移**不再为这些待删除能力设计兼容方案**。
> 2. 未来如果需要 **Custom Channel**，**基于统一事件入口重新设计**，
>    **不要求兼容 DeerFlow 当前 Channel Core**。

**据此移出 MySQL 迁移范围的 5 张表**：

| 表 | 列数 | 引入版本 | 为什么原本在范围内 |
| --- | --- | --- | --- |
| `channel_connections` | 16 | `0001_baseline` | 含 1 个 partial unique index、3 个 JSON 列、advisory lock |
| `channel_credentials` | 9 | `0001_baseline` | 1 个 FK → `channel_connections` |
| `channel_oauth_states` | 11 | `0001_baseline` | 2 个 JSON 列 |
| `channel_conversations` | 9 | `0001_baseline` | 1 个 FK → `channel_connections` |
| `webhook_deliveries` | 5 | `0009_webhook_dedupe` | 🔴 **8448 字节主键**（原唯一确定性硬阻塞）+ 条件 upsert（原最高难度项） |
| **合计** | **50 列** | | |

**同步移出范围的代码 / SQL / 测试**：

| 类别 | 对象 |
| --- | --- |
| **SQL 原语** | `app/channels/dedupe_store.py`（`ON CONFLICT … WHERE … RETURNING` 条件 upsert、`make_interval` ×2）<br>`persistence/channel_connections/sql.py:397-404`（`pg_advisory_xact_lock`） |
| **ORM / 仓储** | `persistence/channel_connections/`、`persistence/webhook_delivery/` |
| **bootstrap 常量** | `_CANONICAL_0019_SCHEMA_FLOOR` 的 5 个条目、`_BASELINE_TABLE_NAMES` 的 4 个条目、`_BASELINE_INDEX_NAMES` 的 **11** 个条目（见 §19 阶段 0）<br>⚠️ **第四轮修正**：这些常量**只服务旧 PostgreSQL bootstrap**，MySQL 侧**不继承**它们（§7.8）—— 因此它们不再是"MySQL 迁移的适配难点"，而是**随 PG backend 删除的 legacy 代码** |
| **配置** | `config/channel_connections_config.py`、`config.yaml:channel_connections` |
| **测试** | ~14 个渠道测试文件 + 9 个"仅提及渠道"的测试需**部分修改**（不是删除）—— 清单见 §19 阶段 0 |

**⚠️ 需要同步澄清的一处文档冲突**：

`backend/app/channels/AGENTS.md` 写有
*"Database tables `channel_connections` and `webhook_delivery` are retained for existing data
and future extensions."* —— 这与 `feature-inventory` 的**删除**决策**不一致**。
本次以 **`feature-inventory` 的产品决策为准**（它是有业务方决策记录的正式文档），
`AGENTS.md` 的该句应在渠道删除实施时一并修正，避免后续读者据此认为"表要保留"。

**🔴 第四轮补充：这 5 张表的"去向"已经明确到实现层**

第三轮只说"移出 MySQL 迁移范围"，但当时仍受"PG 历史 migration 不可变"的约束 ——
`0001_baseline` / `0009_webhook_dedupe` 已经建过这些表，
所以当时的结论是"**MySQL baseline 会建出它们，再写一条 drop migration 删掉**"。
**第四轮取消了这个绕行**：

| | 第三轮的处置 | **第四轮的处置** |
| --- | --- | --- |
| MySQL baseline | `0024` 接在 PG 链后，**含** 5 张渠道表 | **`0001_mysql_baseline` 直接不含**这 5 张表 |
| drop migration | **需要**新增一条（因历史不可变） | ✅ **不需要** —— baseline 就是最终形态 |
| `bootstrap.py` 常量 | 需收缩，且与 `0001_baseline` 的反向 pin 冲突（**难点**） | ✅ 常量**不带入 MySQL**，冲突**不存在**（§7.8） |
| 反向 pin 测试语义 | 需要重新定义 | ✅ **不需要改**（PG 链保持原样，随 PG backend 一起退役） |

> 也就是说：**渠道删除仍然是 MySQL 迁移的"前置条件"（P1）**，
> 但它的**收益从"少 5 张表"升级为"少 5 张表 + 少一条 drop migration + 少一处常量冲突"**。
> 唯一不变的约束是：**渠道删除必须先于 `0001_mysql_baseline` 的编写** ——
> 否则 baseline 会固化一个"含渠道表"的错误起点（§19 阶段 0）。

### 1.6 缩减后的量化对照（第三轮前 → 第三轮后）

| 维度 | 第三轮前 | **第三轮后** | 变化 |
| --- | --- | --- | --- |
| 应用表 | 20 | **15** | −5（−25%） |
| 列 | 274 | **224** | −50（−18%） |
| `sa.JSON` 列 | 25 | **20** | −5 |
| `DateTime(timezone=True)` 列 | 61 | **48** | −13 |
| 库层 FK | 4 | **2** | −2 |
| Partial unique index | 4 | **3** | −1 |
| 索引（`ix_`/`idx_`/`uq_`） | 58 | **47** | −11 |
| 事务级 advisory lock | 3 | **2** | −1 |
| `RETURNING` | 5 | **4** | −1 |
| `ON CONFLICT` | 2 | **1** | −1 |
| `make_interval` | 2 | **0** | −2 |
| **确定性硬阻塞（建表就失败）** | **1** | **0** | ✅ **−1** |
| **High（最高）难度项** | **1** | **0** | ✅ **−1** |

**结论：缩减的是"范围"，不是"性质"。** 三处代价的**结构完全不变** ——
自研 CheckpointSaver、并发语义替换、Schema 规范改造，一件都不会少；
减少的是 Application Data 与 Queue/Scheduler 两个域中**依附于渠道的那部分**。
特别是：**原本唯一的硬阻塞与唯一的高难度项都消失了，但"必须自研 checkpoint saver"这一
唯一可能推翻方案的点，丝毫未变** —— 它才是本方案的风险中心。

> **🔴 第四轮补充**：第四轮**不改变上表任何数字** ——
> 它改变的是**迁移链的组织方式**（独立链 / `0001_mysql_baseline` / bootstrap 简化为单路径）
> 与**迁移的定性**（**fresh-cutover / backend replacement**，而非历史数据迁移）。
> 量化口径仍是 **15 张应用表 / 224 列**（§7.2 的 **20 张含 5 张框架表**）。
> 换句话说：第三轮减的是"**要迁多少**"，第四轮减的是"**怎么迁才最不容易出错**"。
>
> 🔻 **第五轮补充**：上表数字**仍不变**。第五轮减掉的是**不在上表里的东西** ——
> **Store**（整块能力，不是"表里少几列"）、**历史兼容设计**（backfill / 级联 revision / 回滚）、
> **Redis 缓存与索引优化**（本来就不该进这次变更）。
> 因此第三处的措辞需要修正为"自研 CheckpointSaver"（**去掉 Store**）。
> 一句话：**第三轮与第五轮都在减"要迁多少"，但第五轮减的是"本来就不该迁的那部分"。**

### 1.7 第五轮瘦身后的最终迁移范围（**权威口径**）

> 本节是第五轮的**主交付**。所有数字都在**当前 HEAD（`dba975ef`）**上
> 用 `/tmp/schema_dump.py` 重新反射 ORM 元数据 + 全仓 grep 重新统计，
> **不沿用文档此前的旧口径**。

#### A. 权威重算（Channel 删除后的当前 HEAD）

| 维度 | 全量（含渠道） | **裁剪后（渠道已删）** | 说明 |
| --- | --- | --- | --- |
| 应用表 | 20 | **15** | 渠道 5 张整体移出 |
| 列 | 274 | **224** | |
| `sa.JSON` 列 | 25 | **20** | |
| `DateTime(timezone=True)` 列 | 61 | **48** | |
| 库层 FK | 4 | **2** | `user_preferences`、`subagent_batch_items` |
| Partial unique index | 4 | **3** | `runs.uq_runs_thread_active`、`scheduled_task_runs.uq_scheduled_task_run_active`、`users.idx_users_oauth_identity` |
| 索引（`ix_`/`idx_`/`uq_`） | 58（`ix_` 51 / `idx_` 2 / `uq_` 5） | **47（`ix_` 42 / `idx_` 1 / `uq_` 4）** | |
| 未命名唯一约束 | 10 | **8** | |
| `server_default` 列 | 11 | **10** | |
| `nullable=False` 列 | 172 | **137** | |
| `RETURNING`（生产代码） | 5 | **4** | `persistence/run/sql.py:563,594,627` + `scheduled_task_runs/sql.py:211` |
| `ON CONFLICT`（生产代码） | 2 | **1** | `persistence/user/preferences.py:25` 的 `on_conflict_do_update` |
| 事务级 advisory lock | 3 | **2** | `runtime/events/store/db.py:148`、`scheduled_task_runs/sql.py:317` |
| 会话级 advisory lock（bootstrap） | 1 | **1 → 本轮退出范围** | `bootstrap.py:553,559`；第五轮改为**单一 Migrator**（§7.8.5） |
| `with_for_update` | 51 | **51（不变）** | 渠道代码内 **0 处** |
| 命中 `postgres` 的 `.py` 文件 | — | **113**（`packages/harness/deerflow` 48 + `app/` 8 + `tests/` 57） | 旧文档写 111；口径略有差异，**本轮已刷新** |
| PG 专属测试文件 | — | **8** | 文件名含 `postgres` / `pg_` |
| 渠道测试文件（阶段 0 待删） | — | **14** | `tests/` 12 个 + `tests/blocking_io/` 2 个 |
| **框架表** | 6 | **5** | 🔴 第五轮删除 LangGraph Store ⇒ 不再有 `store` / `store_migrations` |

> ⚠️ **一个容易误算的口径**：`community/aio_sandbox/network_proxy.py:198,203` 也用了 `ON CONFLICT`，
> 但那是**本地 SQLite**（`sqlite3` 直连的 `grants` 表），**不属于 MySQL 迁移范围** ——
> 不要把它算进"必须替换的 `ON CONFLICT`"。
>
> ⚠️ **另一个**：`with_for_update` 的 51 处**全部**在非渠道代码里，
> 所以第三轮的范围缩减**没有减少这一项** —— 它仍是迁移期最需要逐个核验的并发面。

#### B. 本轮明确删除的原设计（列出以防"以为还在"）

| # | 被删除的设计 | 原位置 | 现在的处置 |
| --- | --- | --- | --- |
| 1 | **LangGraph Store 自研方案** | 原 §9.4 | ❌ 删除（§9） |
| 2 | **第三方 MySQL Store 评估** | 原 §9.3 | ❌ 删除 |
| 3 | **`ag_store` Schema** | 原 §7.2 映射表 | ❌ 删除（框架表 6 → 5） |
| 4 | **Store migration / health / config / provider 迁移** | 原 §19 阶段 1/2/5 | ❌ 删除 |
| 5 | **pgvector / Store vector 讨论** | 原 §9.5 | ❌ 删除（结论：本来就没启用） |
| 6 | `0024_mysql_baseline` | 第四轮已取消 | ✅ 第五轮**清除全部残留表述** |
| 7 | PG `0001`–`0023` 在 MySQL 上 replay 的分析 | 原 §7.7 | 🟡 保留，但**降级为"为什么必须独立链"的证据**，不再是待解决的冲突 |
| 8 | Existing Instance upgrade | 散落各处 | ❌ 删除（没有既有 MySQL 实例） |
| 9 | backfill migration | 原 §19 阶段 3 第 17 条 | ❌ 删除（没有历史数据要 backfill） |
| 10 | PostgreSQL → MySQL 数据转换 | — | ❌ 删除 |
| 11 | dual-read / dual-write | 原 §20.6 | ❌ 删除（只保留"**配置可切换**" —— 且仅指"尚未切走的职责域可以改回 PG"，**不是数据同步**） |
| 12 | 数据层 rollback 到 PostgreSQL | 原 §20.6 / §19 阶段 5 | ❌ 删除（PG backend 直接废弃） |
| 13 | Channel OAuth lock / inbound dedupe SQL / channel partial unique / 8448 字节主键重设计 / Channel FK cascade / Channel Redis dedupe | 原 §15.6、§18.4 等 | 🟡 作废横幅保留为**历史记录**（未来重建 Custom Channel 时可复用） |
| 14 | **同步 MySQL Checkpointer / Saver** | 原 §8.1、§19 | ❌ 第一阶段不实现（§8.6） |
| 15 | 冗余索引清理 / 索引合并 / 推测 workload 加索引 | 原 §15.4 / §15.5 | ⏭️ 移出本阶段（§15.7） |
| 16 | **bootstrap 分布式锁** | 原 §7.8.5 的"二选一" | ✅ 选"**不做 DB 锁**"（单一 Migrator，§7.8.5） |
| 17 | 未来 Redis cache 的 namespace / invalidation / cascading eviction 设计 | 原 §18.3 | ⏭️ **不提前设计**（§18.3） |

#### C. 当前真正需要迁移的表与运行时组件

**15 张应用表，按"当前是否启用"分三档：**

| 档 | 表 | 列数 | 依据 |
| --- | --- | --- | --- |
| **① 启用（生产主链）** | `runs` | 31 | Run ownership / Run API |
| | `run_events` | 10 | `run_events.backend: db` |
| | `threads_meta` | 10 | `ThreadMetaRepository`（DB 模式） |
| | `users` | 9 | 认证 |
| | `personal_access_tokens` | 9 | PAT 鉴权 |
| | `agents` | 7 | `agent_storage.backend: db` |
| | `managed_subagents` | 5 | 同上 |
| | `scheduled_tasks` | 23 | `scheduler.enabled: true` |
| | `scheduled_task_runs` | 16 | 同上 |
| | `user_preferences` | 3 | Core（UI 偏好） |
| | `projects` | 8 | B 类候选，前端已消费 |
| | `feedback` | 8 | Optional，已装配 |
| | **小计** | **139 列（62%）** | |
| **② 当前关闭（B 类"待评估启用"）** | `mcp_tasks` | **45** | `mcp_tasks.enabled: false` |
| | `subagent_batches` | 17 | `subagent_batches.enabled: false` |
| | `subagent_batch_items` | 23 | 同上 |
| | **小计** | **85 列（38%）** | ⚠️ **最大的剩余瘦身候选**（见 D） |

> 🔴 **必须点明的一处事实**：`mcp_tasks` + `subagent_batches` + `subagent_batch_items`
> **三张表合计 85 列，占裁剪后总列数的 38%**，而它们对应的能力**当前是关闭的**
> （`config.yaml:241` 与 `:106` 均为 `enabled: false`）。
>
> 但 `feature-inventory`（Rev.3，§17.1 E5 / §17.2）**明确要求不要顺手关闭它们**：
> 它们被列为 **B 类"待评估启用"**，理由是"**有明确实现与业务语义**"
> （批量委派运行时 / MCP 长任务运行时），且**带 DB 硬约束**
> （`enabled` 时 `database.backend` 必须是 sqlite/postgres —— `app/gateway/app.py:405`）。
> ⇒ 因此本轮**不擅自删除**，而是把它列为**待你决策的项**（见 D / §21.3（6））。

**运行时组件（必须迁移 / 不迁移）**

| 组件 | 迁移 | 说明 |
| --- | --- | --- |
| Application Data 的 ORM / Repository / JSON / DateTime / Index / FK | ✅ | 15 张表 / 224 列 |
| MySQL async ORM / connection pool | ✅ | `asyncmy`；同步驱动**只为 `SqlAgentStore`** 保留（§20.3） |
| **LangGraph CheckpointSaver（async only）** | ✅ | 4 张框架表：`ag_checkpoint` / `ag_checkpoint_blob` / `ag_checkpoint_write` / `ag_checkpoint_migration` |
| checkpoint / resume / retry / rollback / interrupt | ✅ | 回归基线：`test_delta_channel_checkpointers.py`、`test_run_worker_delta_resume.py` 等 |
| Run / RunEvent | ✅ | `runs`、`run_events` |
| Scheduler | ✅ | `scheduled_tasks`、`scheduled_task_runs` |
| Run ownership / lease | ✅ | `runs` + `runtime/runs/manager.py` |
| 当前仍存在的 PG 专属并发原语 | ✅ | 见 D |
| MySQL 多实例正确性验证 | ✅ | 四个职责域 |
| health / readiness | ✅ | `app/gateway/health.py`（**去掉 Store 探针**） |
| fresh bootstrap | ✅ | 空库 → `0001_mysql_baseline` → 单 head |
| PostgreSQL backend 最终删除 | ✅ | 阶段 6 |
| MCP Tasks / SubAgent Batches | ⚠️ | 表迁移，能力默认关 —— **待决策**（D） |
| **LangGraph Store / `runtime/store/`** | ❌ **删除** | §9 |
| **同步 Checkpointer / Saver** | ❌ **不实现** | §8.6 |

#### D. 剩余 PostgreSQL 专属原语清单（必须逐个替换）

| # | 原语 | 位置 | 处数 | 替代方案 | 难度 |
| --- | --- | --- | --- | --- | --- |
| 1 | **事务级 advisory lock** | `runtime/events/store/db.py:148` | 1 | **V3 实测** → per-thread sentinel row + `FOR UPDATE`，或唯一约束 + bounded retry | **Moderate** |
| 2 | **事务级 advisory lock** | `persistence/scheduled_task_runs/sql.py:317` | 1 | 锁表 sentinel row + `SELECT … FOR UPDATE` | **Moderate** |
| ~~3~~ | ~~会话级 advisory lock（bootstrap）~~ | ~~`bootstrap.py:553,559`~~ | ✅ **本轮退出范围** | **单一 Migrator**，不做 DB 锁（§7.8.5） | — |
| 4 | **`UPDATE … RETURNING`** | `persistence/run/sql.py:563,594,627`、`scheduled_task_runs/sql.py:211` | 4 | 两步法（`FOR UPDATE` + `UPDATE`）；**禁用 `LAST_INSERT_ID(expr)`** | **Moderate** |
| 5 | `ON CONFLICT DO UPDATE` | `persistence/user/preferences.py:25` | 1 | `ON DUPLICATE KEY UPDATE`（MySQL 无 `WHERE`） | Easy |
| 6 | **Partial unique index** | `runs.uq_runs_thread_active`、`scheduled_task_runs.uq_scheduled_task_run_active`、`users.idx_users_oauth_identity` | 3 | Generated Column + Unique Index（§15.3） | **Moderate** |
| 7 | `with_for_update` 的索引依赖 | 全仓 | **51** | 逐个核验索引可用性（InnoDB 缺索引即扩大锁范围） | **Moderate** |
| 8 | 库层 FK | `user_preferences`、`subagent_batch_items` | 2 | 应用层级联 + 孤儿巡检 | Easy |
| 9 | JSON 路径表达式（`->` / `->>`） | `persistence/json_compat.py`（231 行）+ `thread_meta/sql.py` | 1 模块 | 拆 scalar 列 / 应用侧过滤 | **Moderate** |
| 10 | `DateTime(timezone=True)` | 全仓 | **48 列** | UTC `DATETIME(6)` + 应用层边界转换 | Easy（量大） |
| 11 | `sa.JSON` | 全仓 | **20 列** | `TEXT` + 应用侧序列化 | Easy（量大） |
| 12 | **`("sqlite","postgres")` 能力守卫** | `deps.py:84`、`:97`、`:132`、`health.py:229`、`app.py:405`、`agents/__init__.py:47`、`managed_subagents/__init__.py:32` | **7 处** | 全部扩展 `mysql`（否则启动即 `SystemExit` / `ValueError`） | Easy |
| 13 | 驱动 / 方言 `Literal` | `database_config.py:143`、`checkpointer_config.py:9` | 2 处 | 扩展 `mysql` | Easy |
| 14 | PG 系统目录读取 | `0018_oauth_identity_pg_partial.py:50,68` | 1 | **不需要处理**（MySQL 不重放 PG 链） | — |

> 🔴 **第 12 项是第五轮新识别的一处"启动期硬闸门"**：
> 7 处守卫都写着 `("sqlite", "postgres")`，其中 3 处是 **`SystemExit`**
> （`deps.py:84` 的 `scheduler.multi_instance`、`deps.py:97` 的 `GATEWAY_WORKERS`、
> `deps.py:132` 的 `agent_storage`）。**只加驱动不改这些守卫 ⇒ 进程起不来。**
> 这是最容易在实施时漏掉的一类改动（它不报 SQL 错误，而是直接拒绝启动）。

#### E. 仍可进一步删除、而不是迁移的能力（**待你决策**）

| # | 候选 | 规模 | 当前状态 | `feature-inventory` 立场 | 建议 |
| --- | --- | --- | --- | --- | --- |
| **1** | **MCP Tasks**（`mcp_tasks`） | 1 表 / **45 列** | `enabled: false` | B 类"待评估启用"，**不应顺手关闭** | ⚠️ **需决策**：若确认短期不用 ⇒ 整表移出，**直接省 45 列（20%）**，并消除 `with_for_update(read=True)` 的 PG 四档行锁问题 |
| **2** | **SubAgent Batches**（`subagent_batches` + `subagent_batch_items`） | 2 表 / **40 列** | `enabled: false` | 同上（B24） | ⚠️ **需决策**：若移出 ⇒ **再省 40 列（18%）**，并消除 14 处 `skip_locked` |
| 3 | `feedback` | 1 表 / 8 列 | 已装配，Optional | Optional | 可评估（收益小） |
| 4 | `projects` | 1 表 / 8 列 | 前端已消费 | B22 | ❌ **不建议删** |
| 5 | `task_continuity` | 无独立表（走 checkpoint） | `enabled: false` | B 类 | ❌ 无表可省 |
| 6 | `skill_evolution` | 无独立表 | `enabled: false` | — | ❌ 无表可省 |
| 7 | `authorization` | 无独立表 | `enabled: false` | — | ❌ 无表可省 |
| 8 | **同步 Checkpointer / `DeerFlowClient`** | 代码（无表） | **无生产消费者** | TUI 已删 | ✅ **本轮已决定不迁移**（§8.6） |
| 9 | **LangGraph Store** | 1 张框架表 | DB 模式**只构造不读写** | — | ✅ **本轮已决定删除**（§9） |
| 10 | `managed_subagents` | 1 表 / 5 列 | `agent_storage.backend: db` | — | ❌ 与 `agents` 同源，保留 |

> 🔴 **最大的两个候选合计可省 85 列（占 224 的 38%）** ——
> 若确认 B 端 Agent Runtime **当前不需要**"MCP 长任务"与"批量委派"，
> 裁剪后的列数可从 **224 降到 139**，并发原语面同步收窄
> （`mcp_tasks` 贡献 `with_for_update(read=True)` 的 PG 四档行锁问题，
> `subagent_batches` 贡献 14 处 `skip_locked`）。
>
> **但这两项属于"业务能力取舍"，不是工程判断** ⇒
> 本轮**不擅自删除**，只作为**待决策项**提交（§21.3（6））。

---

## 2. Current Database Architecture

### 2.1 一个字段，两条职责链

`backend/packages/harness/deerflow/config/database_config.py:142-209`：

```python
backend: Literal["memory", "sqlite", "postgres"] = "memory"
postgres_url: str = ""
postgres_schema: str = ""
pool_size / pool_recycle / command_timeout / echo_sql
checkpoint_channel_mode: Literal["full", "delta"] = "full"
```

| backend | 应用 ORM（repositories） | LangGraph Checkpointer | LangGraph Store |
| --- | --- | --- | --- |
| `memory` | 内存实现（`get_session_factory()` → `None`） | `InMemorySaver` | `InMemoryStore` |
| `sqlite` | `sqlite+aiosqlite:///{sqlite_dir}/deerflow.db` | `AsyncSqliteSaver` | `AsyncSqliteStore` |
| `postgres` | `postgresql+asyncpg://…` | `AsyncPostgresSaver` | `AsyncPostgresStore` |

**派生关系（`runtime/store/provider.py:62-75`，checkpointer 侧同构）**：

```python
def _resolve_store_config(app_config):
    if app_config.checkpointer is not None:          # ← 独立配置优先
        return app_config.checkpointer
    database = app_config.database
    if database.backend == "memory":   return CheckpointerConfig(type="memory")
    if database.backend == "sqlite":   return CheckpointerConfig(type="sqlite", connection_string=...)
    if database.backend == "postgres": return CheckpointerConfig(type="postgres", connection_string=..., postgres_schema=...)
```

### 2.2 URL 改写与 Schema 注入（纯 PostgreSQL 语义）

| 文件 | 职责 |
| --- | --- |
| `config/database_config.py:282-294` `app_sqlalchemy_url` | 把 `postgresql://` / `postgres://` 改写为 `postgresql+asyncpg://` |
| `config/database_config.py:297-319` `app_sync_sqlalchemy_url` | 改写为 `postgresql+psycopg://`（供同步 `agent_storage.backend: db`）。⚠️ **MySQL 侧是否保留 `PyMySQL` 完全取决于这条同步路径是否仍被使用**（§20.3）；确认不再使用则不要引入 `PyMySQL` |
| `persistence/engine.py:117-131` | `backend == "postgres"` 时**硬性** `import asyncpg`，缺失即抛 `ImportError` |
| `persistence/postgres_schema.py`（全文 258 行） | `CREATE SCHEMA IF NOT EXISTS`、asyncpg `server_settings.search_path`、libpq `options=-c search_path=` 的**分词/合并/转义**、`normalize_libpq_dsn` |
| `config/postgres_schema.py` | `POSTGRES_SCHEMA_PATTERN` + `validate_postgres_schema` |
| `persistence/engine.py:58-92` | `_auto_create_postgres_db`：连到 `postgres` 维护库执行 `CREATE DATABASE` |
| `persistence/migrations/env.py:92-101` | 通过 `deerflow_pg_schema` 注入 search_path |

`postgres_schema.py` 里的 libpq `options` tokenizer（`_split_libpq_options` /
`_join_libpq_options` / `_merge_search_path_option`）是**纯 libpq 语义**，
MySQL 没有对应概念（MySQL 的 schema ≡ database，没有 `search_path`）。

### 2.3 两条连接池，不同生命周期

`database_config.py:14-15` 的注释明确：Postgres 模式下 checkpointer 与 app ORM
**共用同一个数据库 URL，但持有各自独立的连接池**。

- 应用侧：SQLAlchemy `AsyncEngine` + `asyncpg`（`persistence/engine.py:169-182`），
  `pool_size` / `pool_pre_ping` / `pool_recycle` / `command_timeout`（作为 asyncpg `connect_args`）。
- Checkpointer/Store 侧：`psycopg_pool.AsyncConnectionPool`
  （`runtime/checkpointer/async_provider.py:51-76`），带 `autocommit=True`、
  `prepare_threshold=0`、`row_factory=dict_row`、TCP keepalive 参数、`check_connection`。
- 同步路径（TUI / CLI / `DeerFlowClient`）：`PostgresSaver.from_conn_string()` 直连。

### 2.4 当前实际部署形态

| 项 | 现状 | 证据 |
| --- | --- | --- |
| 本机 `config.yaml` | `database.backend: postgres`、`postgres_url: $DATABASE_URL`、`checkpoint_channel_mode: full` | `config.yaml:209-219` |
| 模板默认 | `database.backend: sqlite` | `config.example.yaml:1794-1796` |
| 独立 `checkpointer:` 节 | **未设置**（因此由 `database:` 派生） | `config.yaml` 无此节；`app_config.py:323-329` 默认 `None` |
| Helm | `postgresql.enabled: true`，捆绑 `postgres:16` StatefulSet | `deploy/helm/deer-flow/values.yaml:119-131` |
| Helm 存储 | `postgresql.persistence.accessMode: ReadWriteOnce` | `values.yaml:145` |
| compose | `docker/*.yaml` **不含** postgres 服务 | `grep -rn postgres docker/*.yaml` → 0 命中 |
| 对象存储 | **已落地**（S3/MinIO，`object_storage.enabled: true`，`endpoint_url: http://minio:9000`） | `config.example.yaml:1877-1886` |
| 初始化脚本 | **没有任何 SQL schema / Docker init 脚本**；Schema 由 `Base.metadata.create_all()` + Alembic `stamp`/`upgrade` 生成 | `persistence/bootstrap.py:439-505, 603-701` |

> ⚠️ "没有 SQL 初始化脚本"这一点直接决定本任务 §7 的 Migration 规范如何落地，见 §7.7。

### 2.5 Schema 命名现状

- **20 张应用表全部无前缀**，LangGraph 5 张表也无前缀。
- Alembic 版本表名为默认的 `alembic_version`
  （`persistence/bootstrap.py:361` `SELECT version_num FROM alembic_version`；
  `:416` `"alembic_version" in reflected`）。
- 索引命名三套并存：`ix_` × 51、`idx_` × 2、`uq_` × 5。
- **0 个 TABLE COMMENT、0 个 COLUMN COMMENT**（`Base.metadata` 全部 `comment=None`）。
- **0 个 `ag_` 前缀**。

---

## 3. PostgreSQL Usage Inventory

以下为**实际代码扫描结果**（非 README 推断）。扫描范围：`backend/app`、
`backend/packages`（排除 `tests/`）、`deploy/`、`docker/`、`config*.yaml`、`Makefile`。

> 🔴 **范围提示（第三轮）**：本章的扫描是**在渠道删除之前**做的，因此
> `app/channels/**` 与 `persistence/channel_connections/**` 的命中**仍列在本章**。
> 按 §1.5 的决策，**这些命中不进入 MySQL 迁移范围**。
> 本章保留它们是为了让"原始扫描结果"可追溯；
> **凡涉及渠道的条目，均已标注 `已移出范围`，实施时应以标注为准。**

### 3.1 Driver / 连接（实测版本，非声明版本）

`backend/.venv/lib/python3.12/site-packages/` + `importlib.metadata` 实测：

| 包 | 实测版本 | 声明位置 |
| --- | --- | --- |
| `langgraph` | **1.2.9** | `packages/harness/pyproject.toml` `>=1.2.9,<1.3` |
| `langgraph-checkpoint` | **4.1.1** | 传递依赖 |
| `langgraph-checkpoint-postgres` | **3.1.1** ✅ 已安装 | `pyproject.toml:75` `>=3.1.1,<3.2` |
| `langgraph-checkpoint-sqlite` | **3.1.1** ✅ 已安装 | `pyproject.toml:48` |
| `langgraph-api` | **0.10.0** | — |
| `sqlalchemy` | **2.0.49** | — |
| `alembic` | **1.18.4** | — |
| `asyncpg` | **0.31.0** | `pyproject.toml:74` `>=0.29` |
| `psycopg` / `psycopg-pool` | **3.3.3** / **3.3.0** | `pyproject.toml:76,77` |
| **MySQL 驱动** | **未安装、未声明** | `grep -rniE "mysql\|mariadb\|pymysql\|aiomysql\|asyncmy"` 在 `backend/app`、`backend/packages`、`deploy/`、`docker/`、`config*.yaml` **仅命中 `sandbox/env_policy.py` 的凭据脱敏名单**（`MYSQL_URL` / `MYSQL_PWD` / `MYSQL_PASS`），**与数据库后端无关** |

### 3.2 方言 import

| 位置 | 内容 |
| --- | --- |
| `persistence/user/preferences.py:4-5` | `from sqlalchemy.dialects.postgresql import insert as pg_insert` / `from sqlalchemy.dialects.sqlite import insert as sqlite_insert` |
| `persistence/user/preferences.py:21-25` | `insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert`；`.on_conflict_do_update(index_elements=["user_id","key"], set_={"value": statement.excluded.value})` |
| `app/gateway/auth/repositories/sqlite.py:36-39`（注释） | 依赖 **SQLAlchemy asyncpg 方言包装后的异常形状**（`AsyncAdapt_asyncpg_dbapi.IntegrityError`），并依赖 `sqlalchemy/dialects/postgresql/asyncpg.py::_handle_exception` 的行为 |
| `persistence/migrations/_helpers.py:95-104` | `_EQUIVALENT_TYPE_FAMILIES = (frozenset({"JSON", "JSONB"}),)` —— autogenerate 漂移比较时把 `JSON`/`JSONB` 视为等价 |

### 3.3 裸 SQL / 数据库专有原语（非测试生产代码，逐条）

| 文件:行 | SQL 片段 | 用途 |
| --- | --- | --- |
| `runtime/events/store/db.py:148` | `SELECT pg_advisory_xact_lock(hashtext(CAST(:thread_id AS text))::bigint)` | 事件 `seq` 分配的跨进程串行化（PG 不允许 `SELECT max(...) FOR UPDATE`） |
| `persistence/scheduled_task_runs/sql.py:317-318` | `SELECT pg_advisory_xact_lock(:lock_key)` | 调度器全局并发预算锁（key 常量 `_SCHEDULER_BUDGET_LOCK_KEY = 4694001` 定义在 `sql.py:25`） |
| `persistence/channel_connections/sql.py:398` | ~~`SELECT pg_advisory_xact_lock(:lock_key)`~~ | ~~OAuth scope 串行化~~ → **已移出范围**（随渠道删除，第三轮决策 §1.5） |
| `persistence/bootstrap.py:552` | `SET LOCAL idle_in_transaction_session_timeout = 0` | 防止 DDL 期间被服务端 idle 超时杀掉 |
| `persistence/bootstrap.py:553,559` | `SELECT pg_advisory_lock(:k)` / `pg_advisory_unlock(:k)`，`_PG_LOCK_KEY = 0x0DEE_12F1_0BEE_3682` | 跨进程 Schema bootstrap 互斥 |
| ~~`app/channels/dedupe_store.py:145-162`~~ | ~~`INSERT INTO webhook_deliveries … ON CONFLICT (4-tuple) DO UPDATE … WHERE first_seen < now() - make_interval(secs => :ttl) RETURNING channel`~~ | ~~多 Pod 入站 webhook 去重~~ → **已移出范围**（随渠道删除）。<br>🔴 **这一处原本是全表最难的一处 SQL**（§13.4 评为 **High**），现已整体消失 |
| ~~`app/channels/dedupe_store.py:175`~~ | ~~`DELETE FROM webhook_deliveries WHERE first_seen < now() - make_interval(secs => :ttl)`~~ | ~~惰性清理~~ → **已移出范围** |
| `persistence/scheduled_task_runs/sql.py:203-212` | `UPDATE scheduled_tasks SET last_occurrence_seq = last_occurrence_seq + 1 … RETURNING last_occurrence_seq` | 调度 occurrence 序号分配 |
| `persistence/run/sql.py:552-563` | `UPDATE runs … .returning(RunRow.run_id, RunRow.cancel_action)` | 租约续约 + 原子读取消意图 |
| `persistence/run/sql.py:577-594` | `UPDATE runs … .values(cancel_action=case(...), …).returning(RunRow.cancel_action)` | 首次取消动作胜出 |
| `persistence/run/sql.py:619-627` | `UPDATE runs … .returning(RunRow.run_id)` | "完成仅在取消之前"的原子判定 |
| `persistence/migrations/versions/0018_oauth_identity_pg_partial.py:50,68` | `SELECT indpred FROM pg_index WHERE indexrelid = to_regclass('idx_users_oauth_identity')` | 读 **PostgreSQL 系统目录**判断索引是否为 partial |
| `persistence/json_compat.py:225-227` | `@compiles(JsonMatch)` → `raise NotImplementedError(...)` | 自定义 JSON 匹配编译器，**只支持 sqlite / postgresql** |

### 3.4 JSON 路径表达式（`->` / `->>` 语义）

| 位置 | 表达式 |
| --- | --- |
| `persistence/run/sql.py:207` | `RunRow.metadata_json["regenerate_from_run_id"].as_string()` |
| `persistence/run/sql.py:228-229` | `metadata_json["replay_kind"].as_string()`、`metadata_json["regenerate_from_run_id"]` |
| `persistence/scheduled_task_runs/sql.py:847` | `RunRow.metadata_json["scheduled_task_run_id"].as_string()` |
| `persistence/thread_meta/sql.py:230,247,261` | 经 `json_match()` 生成置顶/归档过滤条件 |

`persistence/json_compat.py`（231 行）为 SQLite 生成 `json_type`/`json_extract`，
为 PostgreSQL 生成 `json_typeof(...)` / `->` / `->>` + `~ '^-?[0-9]+$'` 正则守卫 +
`CAST(... AS BIGINT / DOUBLE PRECISION)`。

### 3.5 索引 / 约束（PostgreSQL 专有）

**3 处 partial unique index**（第三轮后；原 4 处。全部同时声明 `sqlite_where` 与 `postgresql_where`）：

| 索引名 | 表 | 列 | 谓词 | 业务含义 |
| --- | --- | --- | --- | --- |
| `uq_runs_thread_active` | `runs` | `thread_id` | `status IN ('pending','running')` | **每线程至多一个活跃 run**（跨进程） |
| `uq_scheduled_task_run_active` | `scheduled_task_runs` | `task_id` | `status IN ('queued','launching','running')` | 每任务至多一个非终态 occurrence |
| ~~`uq_channel_connection_active_identity`~~ | ~~`channel_connections`~~ | ~~`provider, external_account_id, workspace_id`~~ | ~~`status != 'revoked'`~~ | ⛔ **随渠道删除** |
| `idx_users_oauth_identity` | `users` | `oauth_provider, oauth_id` | `oauth_provider IS NOT NULL AND oauth_id IS NOT NULL` | 每个 OAuth 身份一个账号 |

### 3.6 健康检查

- `app/gateway/health.py:194-215` `_probe_postgres_backend`：
  `psycopg.AsyncConnection.connect(dsn, connect_timeout=…)` + `SELECT 1`，
  复用 `dsn_with_search_path` / `normalize_libpq_dsn`。
- `health.py:218-237` `_probe_checkpointer_backend`：只识别 `("sqlite", "postgres")`，
  其它一律 `DATABASE_UNREACHABLE`（`health.py:229-231`）。
- `health.py:105-127` 明确：探针目标是 **启动时绑定到 `langgraph_runtime` 的实际后端**，
  它可以与 ORM 的 `database:` 后端**不同** —— 再次印证 §1.4 的"两条链已可分离"。

### 3.7 Tests

- 🔻 **113 个测试文件**命中 `postgres`（第五轮重算；第一版为 48，本文档此前记 111）。
- **8 个 PostgreSQL 专属测试文件**：
  `test_pg_schema_integration.py`、`test_persistence_bootstrap_pg_lock.py`、
  `test_multi_worker_postgres_gate.py`、`test_scheduled_task_postgres.py`、
  `test_mcp_task_postgres.py`、`test_migration_0018_oauth_identity_pg_partial.py`、
  `test_persistence_engine_postgres_config.py`、`test_postgres_schema_helper.py`。
- 其中若干测试**逐条断言 SQL 文本**，属于"改实现就红"的强耦合：
  - `test_persistence_bootstrap_pg_lock.py:71-85`：断言 `SET LOCAL idle_in_transaction_session_timeout`
    必须**先于** `pg_advisory_lock`，且退出时必须出现 `pg_advisory_unlock`。
  - `test_run_event_store.py:526`：`assert "pg_advisory_xact_lock" in str(session.execute_calls[0][0])`。
  - `test_inbound_dedupe.py:167-179`：断言 upsert SQL 里必须出现 `ON CONFLICT`、`RETURNING`、
    `make_interval`。
  - `test_thread_meta_repo.py`：断言 `JsonMatch` 在非 sqlite/postgresql 方言下
    `raise NotImplementedError`。

### 3.8 Scripts

`scripts/migrate_agents_to_db.py`、`scripts/migrate_user_isolation.py`、
`scripts/migrate_memory_markdown.py`、`scripts/benchmark/concurrency/worker.py`
均通过 ORM / 仓储接口访问，**不含 PostgreSQL 专有 SQL**。

### 3.9 PostgreSQL 特性使用矩阵（扫描结论）

| 特性 | 是否使用 | 位置 |
| --- | --- | --- |
| JSON / JSONB 列 | ✅ 重度 | 应用 **20 列**（第三轮后；原 25）+ LangGraph `checkpoints.checkpoint/metadata`、`store.value` |
| JSONB 函数 / 运算符 | ✅ | checkpointer `SELECT_SQL`（`jsonb_each_text`）、DeltaChannel 动态列（`checkpoint -> 'channel_versions' ->> %s`）、`json_compat.py`（`json_typeof`、`->`、`->>`、`~`） |
| ARRAY | ✅（仅第三方） | `array_agg(...)`、`ANY(%s)`、`unnest(%s::text[])` |
| BYTEA | ✅（仅第三方） | `checkpoint_blobs.blob BYTEA`、`checkpoint_writes.blob BYTEA NOT NULL`、`bl.channel::bytea` |
| native UUID | ❌ 未使用 | 用户 ID 显式 `String(36)`（`user/model.py` 注释："UUIDs are stored as 36-char strings for cross-backend portability"） |
| ENUM | ❌ 未使用 | 全部状态列为 `String(N)` + 注释枚举值 |
| SET / Geometry / Spatial | ❌ 未使用 | — |
| SERIAL / Sequence | ⚠️ 隐式 | `run_events.id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)` → PG 渲染 `SERIAL`；**无显式 `Sequence`** |
| RETURNING | ✅ **4 处**（原 5） | §3.3（`dedupe_store.py:153` 那处**已随渠道删除**） |
| ON CONFLICT | ✅ **1 处**（原 2）（+1 处 SQLite 本地表） | `user/preferences.py:25`；~~`dedupe_store.py:150`~~ **已随渠道删除**；`community/aio_sandbox/network_proxy.py:198,203` 是**沙箱本地 SQLite 表**，不在迁移范围 |
| CTE | ✅（仅第三方） | PostgresStore 向量搜索、checkpointer DeltaChannel 两阶段 SQL |
| Window Function | ❌ 应用侧未用 | 事件流用 `func.max` 聚合 |
| Partial Index | ✅ **3 处**（原 4） | §3.5（`uq_channel_connection_active_identity` 已随渠道删除） |
| Expression Index | ❌ | 无 |
| GIN / GiST | ❌（应用侧） | PostgresStore 的 `store_prefix_idx` 是 `text_pattern_ops` btree |
| Advisory Lock | ✅ **3 处**（原 4） | §3.3（事务级由 3 降为 2；`channel_connections/sql.py:398` 已随渠道删除） |
| LISTEN / NOTIFY | ❌ 未使用 | — |
| SKIP LOCKED | ✅ | `mcp_tasks/sql.py:230,376,480`、`scheduled_tasks/sql.py:332,530`、`subagent_batches/sql.py:249,263,296,320` |
| FOR UPDATE（含 `read=True`） | ✅ **51 处** | §13.2 |
| `hashtext()` | ✅ | `runtime/events/store/db.py:148` |
| `make_interval(secs => …)` | ✅ **0 处**（原 2） | ~~`dedupe_store.py:152,175`~~ **已随渠道删除** —— 该改写工作整体消失 |
| `SET LOCAL idle_in_transaction_session_timeout` | ✅ | `bootstrap.py:552` |
| `CREATE INDEX CONCURRENTLY` | ✅（仅第三方） | checkpointer 3 条、PostgresStore 2 条 |
| `CREATE EXTENSION` / pgvector | ❌ **未启用** | `PostgresStore` 的 `VECTOR_MIGRATIONS` 从不执行（未传 `index` 配置） |
| `text_pattern_ops` | ✅（仅第三方） | PostgresStore `store_prefix_idx` |
| libpq DSN 语义 | ✅ | `postgres_schema.py` 全文 |
| PG 系统目录（`pg_index` / `to_regclass`） | ✅ | `0018_oauth_identity_pg_partial.py:50,68` |

### 3.10 违反 §6 Query 规范的查询（必须标记）

1. `runtime/events/store/db.py:148` —— 数据库函数 `hashtext()` 承担"线程 → 锁 key"的业务映射。
2. ~~`app/channels/dedupe_store.py:145-175`~~ —— ~~单条 SQL 同时表达"原子获取 + TTL 回收 + 条件更新 +
   返回值判定"，依赖 `now()` / `make_interval` / `ON CONFLICT` / `RETURNING` 四个 PG 语义。~~
   → **已移出范围**（随渠道删除，第三轮决策 §1.5）。
   这一处原本是**全仓最复杂的一处 SQL**（同时依赖 4 个 PG 语义），它的消失是本轮范围缩减中
   **技术价值最高的一项**。
3. `persistence/thread_meta/sql.py:230-261` —— 经 `json_compat.py` 生成的
   `CASE WHEN json_typeof(...) = 'number' AND (col ->> 'k') ~ '^-?[0-9]+$' THEN CAST(... AS BIGINT) END = ?`：
   deeply nested expression + PG 正则 + 类型系统依赖。
4. `persistence/run/sql.py:207,228-229`、`persistence/scheduled_task_runs/sql.py:847` ——
   JSON 路径作为 `WHERE` / `SELECT` 表达式。
5. `persistence/scheduled_task_runs/sql.py:203-212` —— `UPDATE … RETURNING` 作为序号分配原语。
6. LangGraph checkpointer / store 的全部 SQL（第三方，不可改，只能替换）。

> **迁移红线**：不得用 `JSON_UNQUOTE(JSON_EXTRACT(...))` 或 `REGEXP` 复刻这些查询
> —— 那是把 PG 专有 trick 换成 MySQL 专有 trick，同样违反 §6。
> 正确做法是**下沉到应用层**，或按 §7.1 方案 B **拆出 scalar 列**。

---

## 4. Current Schema Inventory

数据来源：用 SQLAlchemy 从 `Base.metadata` 反射（`/tmp/schema_dump.py`，只读、不连库），
以及逐表 `CreateTable(...).compile(dialect=mysql/postgresql)` 的实际渲染结果。

> 🔴 **范围提示（第三轮）**：本章的盘点同样是**在渠道删除之前**做的，
> 因此表数 / 列数 / JSON 数 / FK 数 / partial unique 数 / 索引数**都包含 5 张渠道表**。
>
> | 维度 | 本章数值（含渠道） | **缩减后（实施口径）** |
> | --- | --- | --- |
> | 应用表 | 20 | **15** |
> | 列 | 274 | **224** |
> | `sa.JSON` | 25 | **20** |
> | `DateTime(timezone=True)` | 61 | **47** |
> | FK | 4 | **2** |
> | partial unique index | 4 | **3** |
> | 索引 | 58 | **47** |
>
> **5 张渠道表**（`channel_connections` / `channel_credentials` / `channel_oauth_states` /
> `channel_conversations` / `webhook_deliveries`，合计 50 列）**已随渠道删除**（§1.5）。
> 本章保留它们的原始盘点数据以便追溯，但**凡渠道表的行均已标注**，
> **实施与验收一律以"缩减后"列为准**（完整对照见 §1.6）。
>
> 🔻 **第五轮再减一张框架表**：LangGraph **Store 整体删除**（§9），
> 因此框架表由 **6 张降为 5 张**，总量由 **21 张降为 20 张**（15 应用 + 5 框架）。
> **本章的权威口径以 §1.7 A 为准**；本节的 26 张是**最初的**盘点基线，仅作追溯。

阅读顺序：§4.1 列出物理表 → **§4.2 说明每张表是什么业务** → §4.3 起是类型 / 外键 / 可空 /
键长 / 索引 / 默认值 / 注释的结构盘点。

### 4.1 物理表清单（共 26 张 —— **最初基线，非实施口径**，见 §1.7 A）

**A. 应用表（20 张，由 `Base.metadata` 拥有，274 列）**
—— ⚠️ **其中 5 张渠道表已移出范围，实施口径为 15 张 / 224 列**（见上方范围提示）

| 表名 | 主键 | 列数 | JSON 列 | FK | partial unique |
| --- | --- | --- | --- | --- | --- |
| `agents` | `id` | 7 | 1 | — | — |
| ~~`channel_connections`~~ | ~~`id`~~ | ~~16~~ | ~~3~~ | — | ~~1~~ | **← 已移出范围** |
| ~~`channel_conversations`~~ | ~~`id`~~ | ~~9~~ | — | ~~1~~ | — | **← 已移出范围** |
| ~~`channel_credentials`~~ | ~~`connection_id`~~ | ~~9~~ | — | ~~1~~ | — | **← 已移出范围** |
| ~~`channel_oauth_states`~~ | ~~`state_hash`~~ | ~~11~~ | ~~2~~ | — | — | **← 已移出范围** |
| `feedback` | `feedback_id` | 8 | — | — | — |
| `managed_subagents` | `id` | 5 | 1 | — | — |
| `mcp_tasks` | `id` | 45 | 5 | — | — |
| `personal_access_tokens` | `id` | 9 | 1 | — | — |
| `projects` | `id` | 8 | 1 | — | — |
| `run_events` | `id`（AUTO_INCREMENT） | 10 | 1 | — | — |
| `runs` | `run_id` | 31 | 3 | — | 1 |
| `scheduled_task_runs` | `id` | 16 | — | — | 1 |
| `scheduled_tasks` | `id` | 23 | 1 | — | — |
| `subagent_batch_items` | `id` | 23 | 3 | 1 | — |
| `subagent_batches` | `id` | 17 | 1 | — | — |
| `threads_meta` | `thread_id` | 10 | 1 | — | — |
| `user_preferences` | `(user_id, key)` | 3 | 1 | 1 | — |
| `users` | `id` | 9 | — | — | 1 |
| ~~`webhook_deliveries`~~ | ~~`(channel, workspace_id, chat_id, message_id)`~~ | ~~5~~ | — | — | — | **← 已移出范围** |

> **缩减后（实施口径）**：**15 张应用表 / 224 列**。
> 剩余表中带 partial unique index 的 3 张是 `runs`、`scheduled_task_runs`、`users`；
> 带 FK 的 2 张是 `subagent_batch_items`、`user_preferences`。

**B. 框架表（1 张）**：`alembic_version`

**C. LangGraph 表（5 张，由 `langgraph-checkpoint-postgres` 3.1.1 拥有）**
`checkpoints`、`checkpoint_blobs`、`checkpoint_writes`、`checkpoint_migrations`、`store`
（`migrations/_env_filters.py:28-35` 显式声明前 4 张为 LangGraph 所有，Alembic 必须回避。）

### 4.2 表业务语义总览（这些表分别是什么业务）

> 本节回答"每张表是干什么的、谁在写它、什么时候引入的"。§4.3 起才是结构层面的机械盘点。
> 所有结论来自各 `persistence/*/model.py` 的模块与类注释、`migrations/versions/` 的 revision 名
> 与各 `*Repository` 的归属，不依赖 README。

#### A. 业务域分组

| 业务域 | 表 | 一句话概括 |
| --- | --- | --- |
| 身份与访问 | `users`、`user_preferences`、`personal_access_tokens` | 谁能登录、每个用户的偏好、程序化访问令牌 |
| 会话与运行 | `threads_meta`、`runs`、`run_events`、`feedback` | 一个会话有哪些、每次运行的状态与事件流、用户评价 |
| 项目 | `projects` | 把多个线程归到一个用户项目下 |
| 定时任务 | `scheduled_tasks`、`scheduled_task_runs` | 任务**定义** + 每一次触发的执行记录 |
| 子代理批量 | `subagent_batches`、`subagent_batch_items` | 一次批量派发 + 每个条目的执行状态 |
| MCP 长任务 | `mcp_tasks` | MCP 远程长任务的本地镜像与四条重试通道 |
| 智能体定义 | `agents`、`managed_subagents` | 用户自建智能体、运维统管的子代理 |
| ~~IM 渠道~~ | ~~`channel_connections`、`channel_credentials`、`channel_conversations`、`channel_oauth_states`、`webhook_deliveries`~~ | ⛔ **第三轮决定整体删除**（§1.5）——该业务域不再属于 MySQL 迁移范围 |

> ⚠️ **第三轮范围修正**：上表原为 8 个业务域，**缩减后为 7 个**。
> 渠道域的 5 张表（50 列）随 Channel / GitHub Webhook 一并删除，
> 本文件后续所有"20 张表 / 274 列"口径均已改为 **15 张表 / 224 列**。

#### B. 逐表说明（**15 张应用表**；第三轮已删除 5 张渠道表）

| 表名 | 业务含义 | 归属功能 | 引入版本 |
| --- | --- | --- | --- |
| `users` | **登录账号**：邮箱、密码哈希、OAuth 绑定（`oauth_provider`/`oauth_id`）、`system_role`（`admin`/`user`）、`needs_setup`（首次登录需设密）、`token_version`（改密后使旧令牌失效）。`id` 是 36 字符 UUID **字符串**而非原生 UUID 类型。 | `app/gateway/routers/auth.py`、`app/gateway/auth/` | `0001_baseline` |
| `user_preferences` | **每用户的独立键值偏好**，复合主键 `(user_id, key)`。按 key 拆行是刻意的：类注释写明"独立 key 让并发客户端能补丁式修改互不相交的偏好"而互不覆盖。 | `app/gateway/routers/user_preferences.py` | `0023_user_preferences` |
| `personal_access_tokens` | **API 访问令牌**（`dfp_…` 前缀）：只存 SHA-256 摘要，明文仅出现在创建响应里一次、不落库不落日志；`scopes` 是 `app.gateway.authz` 路由权限的子集；`expires_at`/`revoked_at`/`last_used_at` 管生命周期。 | `app/gateway/routers/auth.py` | `0017_personal_access_tokens` |
| `threads_meta` | **会话（线程）元数据**：展示名、状态、助手、所有者、所属项目、`metadata_json`（其中 `deerflow_pinned`/`deerflow_archived`/`deerflow_project_id` 是前后端约定的跨端键）。`incarnation` 是 0019 预埋的 **expand-only 列，当前无运行时消费方**。 | `app/gateway/routers/threads.py`、`runtime/` | `0001_baseline`；`0019`/`0020` 增列 |
| `runs` | **一次 agent 运行的生命周期记录**：状态机（`pending`/`running`/`success`/`error`/`timeout`/`interrupted`）、模型与调用参数、token 用量（含分模型明细）、列表页便捷字段（首条人类消息 / 末条 AI 消息 / 消息数）、多 worker 所有权（`owner_worker_id` + `lease_expires_at`）、取消请求（`cancel_action` **先到先得**）、幂等键。 | `runtime/runs/`、`app/gateway/routers/runs.py` | `0001_baseline`；`0002`/`0004`/`0005`/`0008`/`0010` 逐步扩展 |
| `run_events` | **run 的事件流**，是 SSE 推送与历史回放的数据源。`(thread_id, seq)` 唯一保证线程内序号连续可比；`category` 的取值与语义由 `runtime/events/catalog.py` 定义（不在库里约束）。 | `runtime/events/store/db.py` | `0001_baseline` |
| `feedback` | **用户对某次 run 的评价**：`rating` = `+1` / `-1`，可选 `comment`；`message_id` 可选，使评价既能针对单条消息也能针对整个 run。`(thread_id, run_id, user_id)` 唯一。 | `app/gateway/routers/feedback.py` | `0001_baseline` |
| `projects` | **用户项目**：线程通过 `threads_meta.project_id` 归属；`instructions` 是用户写的项目级上下文（当前阶段存储并支持 PATCH）。刻意**没有** memory_mode / 共享 / agent 配置列——没有消费方。 | `app/gateway/routers/projects.py` | `0019_projects` |
| `scheduled_tasks` | **定时任务定义**：调度类型与规格（`schedule_type` + `schedule_spec`）、时区、上下文模式（默认 `fresh_thread_per_run`）、重叠策略（`overlap_policy`）、下次运行时间、租约、运行计数、`last_occurrence_seq`（与 occurrence 表配对）。 | `app/gateway/routers/scheduled_tasks.py`、`app/scheduler/service.py` | `0003_scheduled_tasks`；`0022` |
| `scheduled_task_runs` | **定时任务的每一次触发**（occurrence），状态机 `queued`/`launching`/`running`/`success`/`failed`/`skipped`/`interrupted`。`launching` 是**短租约抢占态**，保证多 gateway 实例不会重复启动同一行；`launch_accounted` 标记该次是否已计入父任务配额；`occurrence_seq` 与父任务 `last_occurrence_seq` 配对。 | 同上 | `0003_scheduled_tasks`；`0007`/`0015`/`0022` |
| `subagent_batches` | **一次批量子代理派发的批次头**：`submission_key` 提供幂等、并发上限（`max_live_items`/`max_running_items`/`max_attempts`）、`execution_spec` 执行规格。 | `app/gateway/routers/subagent_batches.py`、`app/subagent_batches/service.py` | `0016_subagent_batches` |
| `subagent_batch_items` | **批次中的每个条目**：prompt、验收标准与裁决（`acceptance_criteria`/`acceptance_verdict`）、状态、尝试次数、租约、结果与截断标志、token 用量。`(batch_id, item_key)` 与 `(batch_id, position)` 双唯一保证条目不重不丢。 | 同上 | `0016`；`0021_batch_acceptance` |
| `mcp_tasks` | **MCP 远程长任务的本地镜像**：四条独立通道各有自己的租约、版本号与尝试计数 —— 派发（`dispatch_*`）、轮询（`next_poll_at`/`poll_*`）、通知回灌（`notification_*`）、取消（`cancel_*`）。`uq_mcp_tasks_user_server_remote` 保证"同一用户 + 同一 MCP server + 同一远端任务"只有一行。 | `app/gateway/routers/mcp_tasks.py`、`app/mcp_tasks/service.py` | `0011_mcp_tasks`；`0012`/`0013`/`0019` |
| `agents` | **用户自建智能体定义**，`(user_id, name)` 唯一。`config` 存完整的 `AgentConfig` 文档（去掉 `name`），`soul` 存人格文本。**用文档列而非逐字段列是刻意的**：`AgentConfig` 将来加字段不需要 schema 变更。有 SQL 与文件两种实现（`SqlAgentStore` / `FileAgentStore`）。 | `app/gateway/routers/agents.py` | `0006_agents` |
| `managed_subagents` | **运维/管理员统管的子代理定义**（存在库里而不是 `config.yaml` 里）：system prompt、工具白名单、技能、模型、轮次与超时。`name` **全局**唯一，且强制禁用 `task`/`ask_clarification`/`present_files` 三个工具。 | `app/gateway/routers/subagents.py` | `0014_managed_subagents` |
| ~~`channel_connections`~~ | ~~用户拥有的 IM 渠道绑定~~ | ⛔ 删除（16 列） | `0001_baseline` |
| ~~`channel_credentials`~~ | ~~渠道的加密凭据~~ | ⛔ 删除（9 列） | `0001_baseline` |
| ~~`channel_conversations`~~ | ~~外部会话 → 内部线程的映射~~ | ⛔ 删除（9 列） | `0001_baseline` |
| ~~`channel_oauth_states`~~ | ~~OAuth 握手的一次性状态~~ | ⛔ 删除（11 列） | `0001_baseline` |
| ~~`webhook_deliveries`~~ | ~~入站 webhook 的跨实例去重表~~ | ⛔ 删除（5 列） | `0009_webhook_dedupe` |

> ⛔ **第三轮决策（取代原 §20.8）：渠道 5 张表整体删除，不做兼容设计。**
> 原 §20.8 曾把 Channel Core 收敛为"Connection + Conversation Mapping + Inbound Dedupe"
> 并规划 `channel_credentials → Credential Broker`、`channel_oauth_states → OAuth Provisioning` 的分阶段去向。
> **该决策已被 feature-inventory 的既有决策取代**（Q2 / Q19 / C7 / C14 / E8），
> 理由与决策来源见 **§1.5**。MySQL 迁移**不再**为这 5 张表设计任何 Schema、索引、
> 主键或 SQL 兼容方案 —— 它们的正确处理是**先删除，再迁移**（§19 阶段 0）。
>
> 未来若需要 Custom Channel，**基于统一事件入口重新设计**，
> 不要求兼容 DeerFlow 当前的 Channel Core（§1.5 架构决策原文）。

#### C. 框架表（6 张，不由应用代码拥有）

| 表 | 业务含义 | 归属 | 迁移相关性 |
| --- | --- | --- | --- |
| `alembic_version` | 当前 schema 版本水位（单列 `version_num`） | Alembic / 本项目 | `persistence/bootstrap.py` 启动时读它判断是否需要迁移（§7.7） |
| `checkpoints` | LangGraph 每个 super-step 的检查点：`checkpoint` 存图状态文档、`metadata` 存步骤来源与写入者；主键 `(thread_id, checkpoint_ns, checkpoint_id)` | `langgraph-checkpoint-postgres` 3.1.1 | 表名不带 `ag_` 前缀，是 §7.1 的命名冲突点 |
| `checkpoint_blobs` | **通道（channel）值的序列化二进制**，按 `(thread_id, checkpoint_ns, channel, version)` 存；`type` 是序列化格式标签（msgpack / pickle） | 同上 | checkpoint 的**主体数据量**在这里，决定 MySQL 侧 `TEXT`/`MEDIUMTEXT` 抉择（§12） |
| `checkpoint_writes` | 每个 task 的**待写记录**（pending writes），支撑中断/恢复与 durable execution；含 `task_path` | 同上 | 与 `checkpoint_blobs` 共同构成 §12 的序列化分析对象 |
| `checkpoint_migrations` | LangGraph **自己的**迁移版本表（单列 `v`），与 Alembic 无关 | 同上 | 别与 `alembic_version` 混为一谈：MySQL 方案要同时交代两套版本体系 |
| `store` | LangGraph `BaseStore` 的键值文档：`prefix` = namespace、`key`、`value`；带 `expires_at`/`ttl_minutes` TTL 列 | `langgraph.store.postgres` | 项目走文件型 memory，该表**基本空置**（§9）；`store_vectors`（pgvector）只在配置 embeddings 时才创建——本项目从未配置，故**根本不存在** |

#### D. 表之间的业务关系（无数据库外键，靠 ID 字段表达）

主干链路：

```
users ──> threads_meta ──> runs ──> run_events
                              └───> feedback
```

完整关联清单（**"库层 FK"一列为"有"的行，就是 §4.5 要求移除的全部外键**；
第三轮删渠道后由 **4 行缩减为 2 行**）：

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
| `subagent_batches` | `users` / `threads_meta` | N:1 | `user_id` / `thread_id` | 无 |
| `subagent_batch_items` | `subagent_batches` | N:1 | `batch_id` | **有**（`ON DELETE CASCADE`） |
| `mcp_tasks` | `users` / `threads_meta` | N:1 | `user_id` / `thread_id` | 无 |
| `mcp_tasks` | `runs` | 0:1 | `run_id` / `notification_run_id` | 无 |
| ~~`channel_connections`~~ | ~~`users`~~ | ~~N:1~~ | ~~`owner_user_id`~~ | ⛔ 表已删除 |
| ~~`channel_credentials`~~ | ~~`channel_connections`~~ | ~~1:1~~ | ~~`connection_id`（同时是主键）~~ | ⛔ **原 FK 已删除** |
| ~~`channel_conversations`~~ | ~~`channel_connections`~~ | ~~N:1~~ | ~~`connection_id`~~ | ⛔ **原 FK 已删除** |
| ~~`channel_conversations`~~ | ~~`threads_meta`~~ | ~~N:1~~ | ~~`thread_id`~~ | ⛔ 表已删除 |
| `personal_access_tokens` | `users` | N:1 | `user_id` | 无 |
| `user_preferences` | `users` | N:1 | `user_id`（**复合主键之一**） | **有**（`ON DELETE CASCADE`） |

`assistant_id` 的取值是 `'lead_agent'` 或自定义智能体名（仅字母/数字/连字符），按**名字**关联到
`agents.name` / `managed_subagents.name`，库层面没有任何约束
（`app/channels/manager.py:217-219` 是唯一的格式校验点 —— 该文件随渠道删除后，
**`assistant_id` 将不再有任何格式校验点**，这是渠道删除带来的一个副作用，
与 MySQL 迁移无关但应记录）。

迁移到 MySQL 后，上表中真实 FK 必须删除（§4.5），所有关联退化为
"应用层保证 + 普通索引"，这也是 §7.5 要求为每个关联字段补索引的原因。

#### E. 五个容易误解的点

1. **`checkpoint_migrations` 不是 Alembic 的表** —— 项目里同时存在两套版本体系（Alembic 管应用表、LangGraph 管 checkpoint 表），MySQL 方案必须同时交代两者的初始化路径（§7.7）。
2. **`managed_subagents` 与 `subagent_batches` 没有关系**：前者是"谁可以当子代理"的**定义**，后者是"一次批量派发"的**执行记录**。
3. **`agents` 与 `managed_subagents` 的差别是管理主体**：前者用户自建、按 `(user_id, name)` 隔离；后者运维统管、`name` 全局唯一。
4. **`threads_meta.incarnation` 与 `mcp_tasks.thread_incarnation` 是 0019 预埋的 expand-only 列，当前无任何运行时消费方**（全仓仅迁移与测试引用）。迁移设计**不要**把它们当作有数据语义的列去做 backfill 方案。
5. **`store` 基本空置、`store_vectors` 不存在** —— 这正是 §9 / §11 对"Store 与向量"给出轻量结论的事实基础，而不是因为"没查到"。

### 4.3 类型分布（**第三轮后 224 列**；原 274 列）

> 实测方式：`Base.metadata` 逐列 `repr(type)` 统计（15 个 `persistence.*.model` 全部 import 后）。
> 括号内为**第三轮删除渠道表前**的原始值。

| 类型 | 列数（第三轮后） | 原值 | 备注 |
| --- | --- | --- | --- |
| `String(N)` | **103** | ~~129~~ | 移除 26 个渠道 String 列后，**全部 ≤ 1024 字符且不再有复合主键越界风险**（§4.7） |
| `DateTime(timezone=True)` | **48** | ~~61~~ | MySQL 渲染 `DATETIME`（无 fsp）→ 亚秒截断 + 时区丢弃（§13.6） |
| `Integer` | **28** | ~~29~~ | 合规 |
| `sa.JSON` | **20** | ~~25~~ | 见 §4.4 |
| `Text` | **19** | ~~24~~ | 见 §12 |
| `Boolean` | 4 | 4 | `users.needs_setup`、`mcp_tasks.result_truncated`、`subagent_batch_items.result_truncated`、`scheduled_task_runs.launch_accounted`（nullable）→ MySQL 落 `TINYINT(1)`，语义等价 |
| `BigInteger` | 2 | 2 | `scheduled_tasks.last_occurrence_seq`、`scheduled_task_runs.occurrence_seq` |
| **合计** | **224** | ~~274~~ | 渠道表共删除 **50 列**（16 + 9 + 11 + 9 + 5） |

> ⚠️ 原版此处把 `DateTime` 记为 61 → 47，**实测应为 61 → 48**：
> 渠道表含 13 个 `DateTime` 列（`channel_connections` 4 + `channel_credentials` 3 +
> `channel_oauth_states` 3 + `channel_conversations` 2 + `webhook_deliveries` 1）。
> 本版已按实测修正（§1.6 同步修正）。

### 4.4 `sa.JSON` 列清单（**第三轮后 20 列**；原 25 列，非 24）

| 表 | JSON 列 | 数量 |
| --- | --- | --- |
| `agents` | `config` | 1 |
| ~~`channel_connections`~~ | ~~`scopes_json`、`capabilities_json`、`metadata_json`~~ | ⛔ 表已删除（3） |
| ~~`channel_oauth_states`~~ | ~~`requested_scopes_json`、`metadata_json`~~ | ⛔ 表已删除（2） |
| `managed_subagents` | `definition` | 1 |
| `mcp_tasks` | `result`、`result_artifact`、`input_required`、`driver_data`、`dispatch_event` | 5 |
| `personal_access_tokens` | `scopes` | 1 |
| `projects` | `presentation` | 1 |
| `run_events` | `event_metadata` | 1 |
| `runs` | `metadata_json`、`kwargs_json`、`token_usage_by_model` | 3 |
| `scheduled_tasks` | `schedule_spec` | 1 |
| `subagent_batch_items` | `acceptance_criteria`、`acceptance_verdict`、`token_usage` | 3 |
| `subagent_batches` | `execution_spec` | 1 |
| `threads_meta` | `metadata_json` | 1 |
| `user_preferences` | `value` | 1 |
| **合计** | | **20**（原 25） |

其中 **nullable 的 JSON 列**（迁移时无 backfill 需求）：
`mcp_tasks.result`、`mcp_tasks.result_artifact`、`mcp_tasks.input_required`、
`mcp_tasks.dispatch_event`、`subagent_batch_items.acceptance_criteria` /
`acceptance_verdict` / `token_usage`、`user_preferences.value`。
（渠道表删除的 5 个 JSON 列**全部非 nullable**（`default=list` / `default=dict`），
因此 nullable JSON 列数量不变。）

### 4.5 外键清单（**第三轮后 2 处**；原 4 处，迁移后必须移除）

| 位置 | 约束 | 语义 |
| --- | --- | --- |
| `persistence/user/model.py:37` | `user_preferences.user_id → users.id` `ON DELETE CASCADE` | 删用户即删偏好 |
| ~~`persistence/channel_connections/model.py:71`~~ | ~~`channel_credentials.connection_id → channel_connections.id`~~ | ⛔ 表已删除 |
| ~~`persistence/channel_connections/model.py:106`~~ | ~~`channel_conversations.connection_id → channel_connections.id`~~ | ⛔ 表已删除 |
| `persistence/subagent_batches/model.py:44` | `subagent_batch_items.batch_id → subagent_batches.id` `ON DELETE CASCADE` | 删批次即删条目 |

> **第三轮效果**：需要处理的 FK 从 4 处降为 **2 处**，且两处都在
> "用户 / 子代理批次"这两个与渠道无关的域里。渠道域的 2 处 FK
> 是 `channel_connections` 的级联删除链，随表删除自然消失。

历史 migration 也创建了同样的 FK，实测分布为：

| revision | FK | 第三轮后 |
| --- | --- | --- |
| `0001_baseline.py:213` | `channel_credentials.connection_id → channel_connections.id` | ⛔ **随表删除消失** |
| `0001_baseline.py:234` | `channel_conversations.connection_id → channel_connections.id` | ⛔ **随表删除消失** |
| `0016_subagent_batches.py:76` | `subagent_batch_items.batch_id → subagent_batches.id` | 保留 |
| `0023_user_preferences.py:18` | `user_preferences.user_id → users.id`（**内联**在 `sa.Column` 里） | 保留 |

> **第三轮的额外收益**：`0001_baseline.py` 里的 2 处 FK **全部属于渠道表**，
> 因此渠道删除后 **`0001_baseline` 将不再包含任何外键** ——
> 这让"MySQL fresh baseline 与 PG 历史基线的一致性核对"（V1）少了一整类差异。

按"历史 migration 不可变"原则，**不修改历史 revision**，
而是在新的 MySQL baseline 中不创建 FK，并让 ORM 模型去掉 `ForeignKey`。

### 4.6 Nullable 清单（**第三轮后 87 个**；原 102 个，按语义分类）

| 类别 | 代表列 | 为什么 nullable | 迁移策略 |
| --- | --- | --- | --- |
| **业务上真可选** | `runs.assistant_id`、`runs.error`、`runs.stop_reason`、`threads_meta.display_name`、`users.oauth_provider`/`oauth_id` | 语义允许缺失 | **保持 nullable** |
| **所有权回填遗留** | `runs.user_id`、`run_events.user_id`、`threads_meta.user_id`、`feedback.user_id` | 认证引入前的历史行 | 先 backfill（§7.3），再切 `NOT NULL` |
| **租约 / 抢占状态** | `runs.owner_worker_id`、`runs.lease_expires_at`、`scheduled_tasks.lease_owner`/`lease_expires_at`、`mcp_tasks.lease_owner`/`lease_expires_at`/`notification_lease_owner`/`notification_lease_expires_at`、`subagent_batch_items.lease_owner`/`lease_expires_at` | `NULL` = "未被占用"，**是业务语义不是缺失** | **保持 nullable** |
| **时间戳（尚未发生）** | `runs.cancel_requested_at`、`scheduled_task_runs.started_at`/`finished_at`、`mcp_tasks.last_polled_at`/`completed_at`/`cancel_requested_at`、`subagent_batches.completed_at` | 事件未发生 | **保持 nullable** |
| **状态机字段（无合理默认）** | `runs.cancel_action`、`scheduled_task_runs.occurrence_seq`、`scheduled_task_runs.launch_accounted`、`mcp_tasks.dispatch_version` | 引入时已存在的行没有值；且 `NULL` 有"未请求"语义 | **先 backfill 再切 NOT NULL**（`cancel_action` 例外，保持 nullable） |
| **隐式 nullable** | 所有 `Mapped[X \| None]` 但未显式写 `nullable=` 的列；以及 `Mapped[str \| None] = mapped_column(String(N))` 这类"无注解也无默认"的列 | SQLAlchemy 从类型注解推导 | 迁移设计**要求逐列显式声明**，不依赖推导 |

**统计**：第三轮后 **224 列中 `nullable=False` 137 个、`nullable=True` 87 个**
（原 274 列中 172 / 102；渠道表删除 50 列，其中 15 个 nullable）。

### 4.7 主键 / 唯一键的 InnoDB 键长审计（**关键发现：第三轮后已无越界表**）

按 utf8mb4（4 字节/字符）估算，InnoDB 上限 **3072 字节**：

| 表 | 键 | 列 | 字节 | 判定 |
| --- | --- | --- | --- | --- |
| ~~**`webhook_deliveries`**~~ | ~~PRIMARY~~ | ~~`channel(64) + workspace_id(512) + chat_id(512) + message_id(1024)`~~ | ~~**8448**~~ | ✅ **已随渠道删除 —— 越界表不再存在** |
| `mcp_tasks` | `uq_mcp_tasks_user_server_remote` | `user_id(64)+server_name(128)+remote_task_id(255)` | 1788 | ✅ |
| ~~`channel_connections`~~ | ~~`uq_channel_connection_owner_provider_identity`~~ | ~~4 列~~ | ~~1408~~ | ✅ 已移出范围 |
| ~~`channel_conversations`~~ | ~~`uq_channel_conversation_connection_external`~~ | ~~3 列~~ | ~~1280~~ | ✅ 已移出范围 |
| `subagent_batches` | `uq_subagent_batches_user_submission` | `user_id(64)+submission_key(256)` | 1280 | ✅ |
| `users` | `ix_users_email` | `email(320)` | 1280 | ✅ |
| ~~`channel_connections`~~ | ~~`uq_channel_connection_active_identity`~~ | ~~3 列~~ | ~~1152~~ | ✅ 已移出范围 |
| `runs` | `uq_runs_idempotency_key` | `idempotency_key(255)` | 1020 | ✅ |
| 其余 25 个键 | — | — | ≤ 768 | ✅ |

> ✅ **第三轮结论：`webhook_deliveries` 随渠道删除后，缩减后的 15 张表
> 没有任何主键 / 唯一键越界。**
> 剩余最大键为 `mcp_tasks.uq_mcp_tasks_user_server_remote` 的 **1788 字节**，
> 距 3072 上限有约 42% 余量。
>
> **因此本项从"确定性硬阻塞"降级为"设计时的核对项"** ——
> 仍应在 `0001_mysql_baseline`（第四轮改名；原 `0024_mysql_baseline`）的 review 中
> 保留这条字节估算检查
> （防止后续新增复合唯一键时重新越界），但**不再需要为主键做任何重设计**。

### 4.8 现有索引命名（**58 个索引 + 未命名唯一约束**；第三轮后实施口径 47 个）
—— ⚠️ **含 11 个渠道索引，缩减后为 47 个**

| 前缀 | 数量 | 说明 |
| --- | --- | --- |
| `ix_` | **51** | SQLAlchemy 自动命名（`ix_<table>_<col>`），规范要求改为 `idx_` / `uk_`<br>其中 **9 个属渠道表**（已移出范围）→ 实施口径 **42** |
| `idx_` | 2 | ~~`idx_channel_connections_event_lookup`~~（已移出范围）、`idx_users_oauth_identity`（后者是 unique）→ 实施口径 **1** |
| `uq_` | 5 | 规范要求改为 `uk_`；其中 ~~`uq_channel_connection_active_identity`~~ 已移出范围 → 实施口径 **4** |
| **未命名 `UniqueConstraint`** | 1 | `managed_subagents.name`（`uc None`）→ 必须补名字 |

> **实施口径合计：47 个索引**（含 1 个未命名约束）。

### 4.9 `server_default` 现状（**第三轮后 10 列**；原 11 列）

| 列 | `server_default` |
| --- | --- |
| `mcp_tasks.result_truncated` | `false` |
| `mcp_tasks.event_version` / `notified_version` / `dispatch_attempt` / `notification_attempt_count` / `cancel_attempt_count` | `'0'` |
| `runs.operation_kind` | `'run'` |
| `runs.token_usage_by_model` | `'{}'` |
| `scheduled_task_runs.attempt_count` | `'0'` |
| `scheduled_tasks.last_occurrence_seq` | `'0'` |
| ~~`webhook_deliveries.first_seen`~~ | ⛔ 表已删除（原为 `now()`） |

**其余大量列只有 Python 侧 `default=`，没有 `server_default`** —— 见 §7.3 的补齐清单。

> 渠道删除只影响 1 个 `server_default`（`webhook_deliveries.first_seen = now()`），
> 它是全部 `server_default` 中**唯一用函数而非字面量**的一个。
> 删掉它之后，**剩余的 10 个 `server_default` 全是字面量常量**，
> MySQL 与 PG 的渲染差异面进一步收窄（无需再讨论 `now()` 的方言差异）。

### 4.10 COMMENT 现状

**0 个 TABLE COMMENT、0 个 COLUMN COMMENT。** **第三轮后 15 张表**的 `Base.metadata`
全部 `comment=None`，**224 列**的 `comment` 全部为空（原为 20 张表 / 274 列）。
MySQL 的 `COMMENT` 是表/列定义的一部分，需要补齐的对象清单见 §7.4 ——
渠道删除使需要补 COMMENT 的对象从 **20 表 + 274 列降为 15 表 + 224 列**。

---

## 5. PostgreSQL-specific Dependencies（依赖与代码耦合汇总）

### 5.1 硬依赖（不替换就跑不起来）

| # | 依赖 | 证据 | MySQL 侧 |
| --- | --- | --- | --- |
| 1 | `asyncpg`（应用 ORM 异步驱动） | `persistence/engine.py:117-131` 强制 import | 替换为 **`asyncmy`**（异步主路径，§20.3）；**不预置** `aiomysql` / `PyMySQL` |
| 2 | `psycopg` + `psycopg-pool`（checkpointer/store/健康检查/bootstrap 锁） | `runtime/checkpointer/async_provider.py:53-54,96`、`app/gateway/health.py:194-215` | 需替换 |
| 3 | `langgraph-checkpoint-postgres` | `runtime/checkpointer/{provider,async_provider}.py` | **需自研** |
| 4 | `PostgresStore` / `AsyncPostgresStore` | `runtime/store/{provider,async_provider}.py` | **需自研** |

### 5.2 语义依赖（能替换，但要重新证明正确性）

见 §3.3 / §3.4 / §3.5 与 §13。

### 5.3 纯 PostgreSQL 概念（MySQL 无对应物）

| 概念 | 位置 | MySQL |
| --- | --- | --- |
| `CREATE SCHEMA` + `search_path` | `postgres_schema.py` 全文、`migrations/env.py:92-101` | MySQL 的 schema ≡ database，无 `search_path` |
| libpq `options=-c key=value` | `postgres_schema.py:46-126` | 无对应概念 |
| `SET LOCAL idle_in_transaction_session_timeout` | `bootstrap.py:552` | 无此变量 |
| 事务级 advisory lock | §3.3 | 无等价物 |
| PG 四档行锁强度（`FOR KEY SHARE`/`FOR SHARE`/`FOR NO KEY UPDATE`/`FOR UPDATE`） | `mcp_tasks/sql.py:166-169` 的注释**显式依赖**该模型 | MySQL 只有 S/X 锁 + 间隙锁 |
| PG 系统目录（`pg_index` / `to_regclass` / `indpred`） | `0018_oauth_identity_pg_partial.py:50,68` | 需改为 `information_schema.STATISTICS` 或直接跳过 |

---

## 6. MySQL 8.0.24 Compatibility

### 6.1 版本能力核对（严格以 8.0.24 为准）

| 能力 | 8.0.24 | 说明 |
| --- | --- | --- |
| `SELECT … FOR UPDATE` / `FOR SHARE` / `SKIP LOCKED` / `NOWAIT` | ✅ | 8.0.1+ |
| CTE / 窗口函数 | ✅ | 8.0.1+ / 8.0.2+ |
| 函数索引（表达式索引） | ✅ | 8.0.13+ |
| 生成列 `GENERATED ALWAYS AS (…) STORED` | ✅ | 5.7+ |
| BLOB/TEXT/JSON 的**表达式默认值** `DEFAULT (expr)` | ✅ | **8.0.13+**（8.0.24 满足）。⚠️ 8.0.13 之前 TEXT 不能有默认值 |
| `INSERT … ON DUPLICATE KEY UPDATE` | ✅ | 但**不支持** `DO UPDATE … WHERE <cond>` |
| 行别名 `INSERT … AS new` | ✅ | 8.0.19+ |
| `LAST_INSERT_ID(expr)` | ✅ | 可用于序号分配（但见 §13.4 的污染风险） |
| `GET_LOCK` / `RELEASE_LOCK` | ✅ 存在，但**不采用** | **连接级**，非事务级 → 不能替代 PG 事务级 advisory lock；按 §20.7 决策**不作为业务锁方案**（连接归还池时锁不释放） |
| `SELECT max(…) … FOR UPDATE` | ✅ **允许** | 实测 SQLAlchemy 编译通过（第一版误判为"行为不同"） |
| **`RETURNING`** | ❌ | **不支持**（MariaDB 有）。硬阻塞 |
| **Partial / Filtered Index** | ❌ | 需生成列或函数索引 workaround |
| **事务级 Advisory Lock** | ❌ | 无等价物 |
| `CREATE INDEX CONCURRENTLY` | ❌ | 语法不存在；8.0 InnoDB 默认 online DDL（`ALGORITHM=INPLACE, LOCK=NONE`） |
| `idle_in_transaction_session_timeout` | ❌ | 只有 `innodb_lock_wait_timeout`（默认 50s）、`wait_timeout`，语义不同 |
| `BOOLEAN` | ⚠️ 别名 | = `TINYINT(1)` |
| `DATETIME` 时区 | ❌ | 无时区感知；`TIMESTAMP` 会做 UTC 换算且 2038 上限 |
| `DATETIME` 亚秒精度 | ⚠️ 需显式 | 默认 `DATETIME`（fsp=0），需 `DATETIME(6)` |
| InnoDB 索引键长上限 | ⚠️ 3072 字节 | utf8mb4 下单列 ≤ 768 字符 |
| `TEXT` 作主键 | ❌ | 主键不能用前缀长度 |
| 默认隔离级别 | ⚠️ REPEATABLE READ | PG 默认 READ COMMITTED |
| 唯一冲突错误 | ⚠️ `1062` | PG 是 SQLSTATE `23505` |
| 死锁错误 | ⚠️ `1213` | PG 是 `40P01` |

### 6.2 结论

**MySQL 8.0.24 足以承载 Application Data 层的全部查询模式**，前提是：

- 放弃 `RETURNING` → `rowcount` 判定 或 `SELECT … FOR UPDATE` + `UPDATE` 两步法（§13.4）；
- 放弃 partial index → 生成列唯一索引（§15.3，语义可**精确等价**）；
- 放弃 advisory lock → 锁表 sentinel row + `SELECT … FOR UPDATE`，或"唯一约束 + 重试"；
- 把 **20 个** JSON 列（第三轮后；原 25）改为 `TEXT` + 应用层序列化（其中 2 张表的过滤键拆 scalar 列）；
- 所有时间列显式 `DATETIME(6)`，并在应用层统一 UTC-naive 写入边界。

**Checkpoint / Store 层无法通过配置迁移**，因为第三方包的 SQL 与 DDL 在 MySQL 上不可执行。

---

## 7. MySQL Rule Compliance Analysis

### 7.1 数据类型规则（§1.1）

| 规范 | 当前用法 | 合规改造方案 |
| --- | --- | --- |
| 禁止 `JSON` | **20 个应用列**（第三轮后；原 25）+ LangGraph `checkpoints.checkpoint`/`metadata` + `store.value` | **方案 A（TEXT 序列化）**：`sa.JSON` → `sa.Text` + 应用层 `json.dumps(ensure_ascii=False)`。项目**已有**这个模式：`persistence/engine.py:25-27` 的 `_json_serializer`、`runtime/events/store/db.py:80-89` 的 `_content_to_db`（JSON 文本 + `content_is_json` 类型标记） |
| 禁止 `BLOB` 系列 | `checkpoint_blobs.blob BYTEA`、`checkpoint_writes.blob BYTEA NOT NULL` | 见 §12.4 方案 A/C；**这是最困难的一项** |
| 禁止 `ENUM` | 未使用 | 无需改造（现有 `String(N)` 已合规） |
| 禁止 `SET` / Geometry / Spatial | 未使用 | — |
| 禁止其它 specialized types | `BINARY(16)`（仅第三方 MySQL 包） | 第三方包不合规，见 §16.2 |
| 优先 `VARCHAR`/`TEXT`/`CHAR` | 已普遍使用 `String(N)`/`Text` | 需补键长审计（§4.7）与 `TEXT` 默认值写法（表达式形式） |
| 优先 `INT`/`BIGINT` | `run_events.id`、token 计数、`occurrence_seq` | 合规 |
| 优先 `BOOLEAN` | `users.needs_setup`、`mcp_tasks.result_truncated`、`subagent_batch_items.result_truncated` | MySQL 落 `TINYINT(1)`，语义等价 |
| 优先 `DATETIME` | **48 个** `DateTime(timezone=True)`（第三轮后；原 61） | **必须显式 `DATETIME(6)`**，且应用边界统一 UTC（§20.10 #1） |
| `TEXT` vs `MEDIUMTEXT`/`LONGTEXT` | checkpoint blob 需要 >64KB | 规范的禁用清单**未包含**它们（属 TEXT 家族），"优先使用"清单只列了 `TEXT`。**已决策**（§20.5 / §20.10）：**默认 `TEXT`；仅 checkpoint serialized payload 允许 `MEDIUMTEXT`；`LONGTEXT` 不采用**；超出 `checkpoint.inline_blob_threshold` 的走对象存储 |

**JSON 列的三条落地路线对比**

| 方案 | 适用列 | 优点 | 缺点 |
| --- | --- | --- | --- |
| **A. TEXT 序列化** | 20 列中的 17 列（原 25 中的 22） | 零语义损失、迁移机械、应用层已有序列化习惯 | 失去 DB 侧 JSON 查询能力 —— 但**现有代码本来就依赖 `json_compat.py` 这种方言 hack**，改成应用层过滤反而更符合 §6 |
| **B. Schema 规范化** | 结构稳定且需要过滤/排序的少量列 | 真正的索引化查询 | 需逐列设计；`agents.config`、`scheduled_tasks.schedule_spec`、`subagent_batches.execution_spec` 这类"整文档往返"字段**明确不应规范化**（`agents/model.py` 的注释解释了原因：新增 `AgentConfig` 字段必须零 Schema 变更地往返） |
| **C. 外部存储** | 大 payload：checkpoint blob、`mcp_tasks.result_artifact`、`subagent_batch_items.result` | DB 保持轻量 | 引入"对象存储写 + DB 写"的部分失败与孤儿 GC |

**推荐组合**

- **20 列全部落 `TEXT`**（方案 A）。
- **另有 2 张表的 3 个过滤 key 额外拆出 scalar 列**（方案 B）——注意这不是"替换 TEXT"，
  而是**在保留 `metadata_json` TEXT 的同时增加可索引的 scalar 列**：
  - `threads_meta.metadata_json` → 增 `pinned` / `archived`（+ 保留 `metadata_json` TEXT 承载其余字段）；
  - `runs.metadata_json` → 增 `regenerate_from_run_id` / `replay_kind` / `scheduled_task_run_id`
    （**这三个恰好是唯一被过滤的 key**，见 §3.4）。
- **checkpoint 大 blob → 方案 C**，见 §12.4。

### 7.2 Table / Column 规范（§2）

**表名前缀 `ag_`** —— 现状 **0 张表带前缀**。
**第五轮后完整映射（20 张 = 15 张应用表 + 5 张框架表；原 26 张）**：

| 现名 | 目标名 | 现名 | 目标名 |
| --- | --- | --- | --- |
| `agents` | `ag_agents` | `scheduled_tasks` | `ag_scheduled_tasks` |
| `feedback` | `ag_feedback` | `scheduled_task_runs` | `ag_scheduled_task_runs` |
| `managed_subagents` | `ag_managed_subagents` | `subagent_batches` | `ag_subagent_batches` |
| `mcp_tasks` | `ag_mcp_tasks` | `subagent_batch_items` | `ag_subagent_batch_items` |
| `personal_access_tokens` | `ag_personal_access_tokens` | `threads_meta` | `ag_threads_meta` |
| `projects` | `ag_projects` | `users` | `ag_users` |
| `run_events` | `ag_run_events` | `user_preferences` | `ag_user_preferences` |
| `runs` | `ag_runs` | `alembic_version` | `ag_alembic_version` |
| | | `checkpoints` | `ag_checkpoint` |
| | | `checkpoint_blobs` | `ag_checkpoint_blob` |
| | | `checkpoint_writes` | `ag_checkpoint_write` |
| | | `checkpoint_migrations` | `ag_checkpoint_migration` |

> ⛔ **第五轮删除的 1 个映射**：`store` → ~~`ag_store`~~ —— **LangGraph Store 整体不迁移**（§9），
> 因此不再需要 `ag_store` 这个名字，也不需要 `store_migrations`。
> 框架表由 **6 张降为 5 张**（`ag_checkpoint` / `ag_checkpoint_blob` / `ag_checkpoint_write` /
> `ag_checkpoint_migration` / `ag_alembic_version`）。
>
> ⛔ **第三轮删除的 5 个映射**（不再需要改名，因为表本身要删）：
> `channel_connections` / `channel_credentials` / `channel_oauth_states` /
> `channel_conversations` / `webhook_deliveries`。
> 映射表由 26 张缩为 **20 张**，需要改 `__tablename__` 的位置从 20 处降为 **15 处**。

**必须同步修改的位置**（否则运行期不一致）：

| 位置 | 内容 |
| --- | --- |
| ORM | `__tablename__`（**15 处**；原 20 处，见 §4.1 的 `grep` 结果） |
| Bootstrap 常量 | 🟡 **legacy（第四轮）**：`_CANONICAL_0019_SCHEMA_FLOOR`（`bootstrap.py:134-171`，**20 张表名 + 274 个列名硬编码**）、`_BASELINE_TABLE_NAMES`（`:198-210`）、`_BASELINE_INDEX_NAMES`（`:215-249`）**均不为 MySQL 服务** —— 保持原样、随 PG backend 删除（§7.8）。MySQL 侧**不新增**同类常量，也**不做"缩减"** |
| Alembic 版本表 | 🔴 **这是 MySQL 侧必须改的一处**：`bootstrap.py:361` `SELECT version_num FROM alembic_version`、`:416` `"alembic_version" in reflected`、`_alembic_safe_url`/`_get_alembic_config`（`:290-327`）—— `version_table` **当前未设置**（默认 `alembic_version`）。MySQL 独立链需**显式指定独立的 `version_table`**（§7.8） |
| LangGraph 表过滤 | `migrations/_env_filters.py:28-35` `LANGGRAPH_OWNED_TABLES` |
| ~~Raw SQL~~ | ⛔ ~~`app/channels/dedupe_store.py:147,175,194`（`webhook_deliveries`）~~ —— **随渠道删除消失**，MySQL 侧不再需要处理这 3 处裸 SQL |
| Migration | 新 revision 中的 `op.create_table` / `op.add_column` 的 `table=` 参数 |
| Health check | `app/gateway/health.py`（若新增 `mysql` 探针） |
| Tests | 🔻 **113 个**命中 `postgres` 的文件（第五轮重算；原记 111）；8 个 PG 专属文件（渠道测试删除后需重新点数，见 §19 阶段 0 组 C） |

> 🟡 ~~🔴 **`bootstrap.py` 的三个常量是本阶段唯一的真实设计难点。**~~
> **【第四轮改写：难点已消失，改为 legacy 标记】**
>
> 上一版把这里判为"难点"，前提是 **MySQL 与 PG 共用一条 Alembic 链** ——
> 那样 `0001_baseline` 必然会创建渠道表，于是常量必须"反向 pin 住 0001 的产物"，
> 而渠道删除又与 `0001` 的不可变性冲突，只能靠新增 drop migration + 重新定义 pin 测试语义来解。
>
> **第四轮取消共用链后，这个前提不成立**：
> - MySQL 走**独立链**（§7.8），`0001_mysql_baseline` 是 **MySQL 自己的链根**，
>   它**直接生成裁剪后的 15 张表**，不创建任何渠道表；
> - 于是**没有 drop migration 要写**，**没有 pin 冲突要解**；
> - `_CANONICAL_0019_SCHEMA_FLOOR` / `_BASELINE_TABLE_NAMES` / `_BASELINE_INDEX_NAMES`
>   以及 `_FORWARD_COMPATIBLE_REVISION` / `_validate_forward_schema` /
>   `_run_baseline_create_all_sync`，**全部只服务旧 PostgreSQL bootstrap**。
>
> 🔴 **处置：一律标记为 legacy，不带入 MySQL，随 PostgreSQL backend 一并删除。**
> 逐项清单、删除时点与理由见 **§7.8**。
> （PG 链上的 `0001_baseline` 与其 pin 测试**保持原样、不动** ——
> 它们仍是 PG 侧的事实，只是 MySQL 侧不再引用它们。）

**主键**：**15 张表全部已有 PK**，合规。注意：

- ~~🔴 `webhook_deliveries` 的 PK 键长 8448 字节 > 3072，必须重新设计（§4.7）。~~
  ⛔ **第三轮后本项消失**：该表随渠道删除，`delivery_key CHAR(64)` 的
  surrogate-key 重设计（原 §20.8 决策）**不再需要实施**。
  §15.6 保留该分析仅作为历史记录。
- `ag_checkpoint*` 的 `thread_id` / `checkpoint_ns` / `checkpoint_id` / `channel` / `version`
  **必须从 `TEXT` 收紧为 `VARCHAR(N)`**（MySQL 不允许 `TEXT` 作主键）。
  其中 **`checkpoint_ns` 的长度仍待实测**（§20.9 / §20.11 V2），暂以 `VARCHAR(255)` 占位。

**禁止 FK**：**2 处 FK** 需移除（§4.5；原 4 处），并在新 baseline 中不创建。
`user_preferences` / `subagent_batch_items` 的级联删除需要**移到应用层**
（现在依赖 `ON DELETE CASCADE`）—— 这是一处**功能回归点**，
必须显式实现级联删除逻辑并配孤儿巡检。
（原列表中的 `channel_credentials` / `channel_conversations` 随表删除，
**不再需要为它们写应用层级联** —— 这是第三轮省下的一处实现工作量。）

**Relationship 索引**（FK 移除后必须显式建立）：

| 关系 | 现状索引 | 目标 |
| --- | --- | --- |
| ~~`channel_credentials.connection_id → channel_connections.id`~~ | ~~PK `connection_id` 已覆盖~~ | ⛔ 表已删除 |
| ~~`channel_conversations.connection_id → channel_connections.id`~~ | ~~`ix_channel_conversations_connection_id`~~ | ⛔ 表已删除 |
| `user_preferences.user_id → users.id` | PK `(user_id, key)` leftmost prefix | 无需新增 |
| `subagent_batch_items.batch_id → subagent_batches.id` | `ix_subagent_batch_items_batch_id` | `idx_subagent_batch_items_batch_id` |

### 7.3 NOT NULL / DEFAULT（§3）

**必须补 backfill 再切 `NOT NULL` 的列**（无合理默认值，**禁止制造虚假默认**）：

| 列 | 现状 | Backfill 策略 |
| --- | --- | --- |
| `ag_runs.user_id` | nullable（认证前数据） | 按 `thread_id` 关联 `ag_threads_meta.user_id` 回填；`scripts/migrate_user_isolation.py` 有同类逻辑可参考；仍有孤儿行则保留 nullable 并记录 |
| `ag_run_events.user_id` | nullable | 同上 |
| `ag_threads_meta.user_id` | nullable | 无法回填时保留 nullable |
| `ag_feedback.user_id` | nullable | 按 `thread_id` 回填 |
| `ag_scheduled_task_runs.occurrence_seq` | nullable（0022 引入） | 对历史行按 `(task_id, created_at, id)` 排序生成序号后回填 |
| `ag_scheduled_task_runs.launch_accounted` | nullable（0022 引入） | 按 `run_id IS NOT NULL` 推导后回填 |
| `ag_mcp_tasks.dispatch_version` | nullable（0013 引入） | 按 `dispatch_event IS NULL` 推导语义默认值后回填 |

**必须补 `server_default` 的列**（现在只有 Python 侧 `default=`，DDL 无默认值，
对 DBA 手工执行 migration 的场景不友好）：

| 表 | 列 | 目标 `server_default` |
| --- | --- | --- |
| `ag_runs` | `status` | `'pending'` |
| `ag_runs` | `multitask_strategy` | `'reject'` |
| `ag_runs` | `message_count`、`total_input_tokens`、`total_output_tokens`、`total_tokens`、`llm_call_count`、`lead_agent_tokens`、`subagent_tokens`、`middleware_tokens` | `0` |
| `ag_run_events` | `content` | `''` ⚠️ MySQL 的 `TEXT` 默认值必须写成**表达式形式** `DEFAULT ('')`（8.0.13+） |
| `ag_run_events` | `event_metadata` | `'{}'`（TEXT 序列化后，同样需表达式形式） |
| `ag_threads_meta` | `status` | `'idle'` |
| `ag_threads_meta` | `metadata_json` | `'{}'` |
| `ag_agents` | `config` | `'{}'`；`soul` | `''`（TEXT，表达式形式） |
| `ag_projects` | `instructions` | `''`（TEXT，表达式形式）；`presentation` | `'{}'`；`status` | `'active'` |
| ~~`ag_channel_connections`~~ | ⛔ 表已删除（原需补 `status`/`external_account_id`/`workspace_id`/3 个 JSON 列的默认值） | — |
| ~~`ag_channel_oauth_states`~~ | ⛔ 表已删除（原需补 `requested_scopes_json`/`metadata_json`） | — |
| `ag_managed_subagents` | `definition` | `'{}'` |
| `ag_mcp_tasks` | `status` | `'pending'`（按现有状态机语义确认）；`driver_data` | `'{}'`；`notification_status` | `'none'`；`result_truncated` | `false`；`poll_attempt_count`/`consecutive_poll_error_count` | `0` |
| `ag_personal_access_tokens` | `scopes` | `'[]'` |
| `ag_projects` | `user_id` 等 | 已有 `NOT NULL`，无需 |
| `ag_scheduled_tasks` | `status` | `'enabled'`；`overlap_policy` | `'enqueue'`；`context_mode` | `'fresh_thread_per_run'`；`run_count` | `0` |
| `ag_subagent_batch_items` | `attempt` | `0`；`result_truncated` | `false` |
| `ag_users` | `system_role` | `'user'`；`needs_setup` | `false`；`token_version` | `0` |

**保持 nullable 的列**（`NULL` 有明确业务语义，**不得**为了"全部 NOT NULL"而造默认值）：
§4.6 第 3、4 行（全部 lease / owner / 时间戳列）+ `runs.cancel_action`（`NULL` = 无取消请求）。

### 7.4 中文 COMMENT（§4）

**现状：0 个 TABLE COMMENT、0 个 COLUMN COMMENT。**
MySQL 的 `COMMENT` 是表/列定义的一部分，应在 `create_table` / `add_column` 时写入
（`ALTER TABLE … COMMENT` 在 8.0 是 in-place，但 `MODIFY COLUMN … COMMENT` 对某些类型是 copy 操作）。

**需要在迁移设计中补齐注释的对象（清单）**

| 表 | 必须注释的表级说明 | 必须注释的关键列（示例，非全量） |
| --- | --- | --- |
| `ag_runs` | "Agent 运行记录：状态机、token 统计、多 Worker 租约、取消意图" | `status`（枚举语义 pending/running/success/error/timeout/interrupted）、`operation_kind`、`multitask_strategy`、`owner_worker_id`（多 Worker 租约持有者）、`lease_expires_at`、`cancel_action`、`cancel_requested_at`、`stop_reason`、`metadata_json`（含 `regenerate_from_run_id`/`replay_kind`/`scheduled_task_run_id`）、`token_usage_by_model`、`follow_up_to_run_id` |
| `ag_run_events` | "线程内事件流：单调递增 seq 分配" | `seq`（线程内单调递增，由事务锁保证）、`category`、`event_type`、`content`（可能是 JSON 字符串，见 `content_is_json` 标记）、`event_metadata` |
| `ag_threads_meta` | "线程元数据（含化身 incarnation 与置顶/归档）" | `incarnation`、`status`、`project_id`、`metadata_json` |
| `ag_scheduled_tasks` | "定时任务定义" | `schedule_type`/`schedule_spec`、`overlap_policy`、`context_mode`、`lease_owner`、`last_occurrence_seq` |
| `ag_scheduled_task_runs` | "调度 occurrence 队列" | `occurrence_seq`、`launch_accounted`、`attempt_count`、`lease_owner`、`scheduled_for`、`trigger` |
| `ag_mcp_tasks` | "远程 MCP 任务轮询状态机" | `remote_task_id`、`driver_name`、`notification_status`、`dispatch_version`、`event_fingerprint`、`next_poll_at`/`next_cancel_at`/`next_notification_at`、`input_required`、`thread_incarnation` |
| `ag_subagent_batches` / `ag_subagent_batch_items` | "批量子 Agent 提交与条目" | `submission_key`、`execution_spec`、`acceptance_criteria`、`acceptance_verdict`、`token_usage`、`item_key`、`position`、`lease_owner` |
| `ag_users` / `ag_user_preferences` | "账号与逐 key 偏好" | `system_role`、`needs_setup`、`token_version`、`oauth_provider`/`oauth_id`、`value` |
| `ag_agents` | "自定义 Agent 定义（整文档往返）" | `config`（`AgentConfig` 文档减 `name`）、`soul` |
| `ag_projects` | "项目分组" | `presentation`、`instructions` |
| ~~`ag_channel_*`（4 张）~~ | ⛔ 表已删除 | — |
| `ag_personal_access_tokens` | "个人访问令牌" | `token_digest`（哈希，非明文）、`scopes`、`revoked_at` |
| `ag_feedback` | "用户反馈" | `rating`、`message_id` |
| ~~`ag_webhook_deliveries`~~ | ⛔ 表已删除 | — |
| `ag_managed_subagents` | "托管子 Agent 定义" | `definition` |
| `ag_checkpoint` | "LangGraph checkpoint 头（**框架表，禁止手工修改**）" | `thread_id`、`checkpoint_ns`、`checkpoint_id`、`parent_checkpoint_id`、`type`、`checkpoint`（序列化格式说明）、`metadata` |
| `ag_checkpoint_blob` | "LangGraph 通道值 blob" | `channel`、`version`、`type`（序列化类型标签）、`blob`（**序列化格式与大小上限说明**） |
| `ag_checkpoint_write` | "LangGraph 待处理写入" | `task_id`、`idx`、`task_path`、`blob` |
| `ag_checkpoint_migration` | "LangGraph 自身 DDL 版本表" | `v` |
| `ag_store` | "LangGraph Store 文档（当前仅 agent 定义命名空间）" | `prefix`（namespace）、`key`、`value` |
| `ag_alembic_version` | "Alembic 迁移版本（**框架表**）" | `version_num` |

> 注释语言：全部中文（表级 + 列级），枚举列的注释必须列出**全部合法取值及其语义**。

### 7.5 Index 规范（§5）

见 §15（独立章节）。核心结论：

- **47 个索引**需要按 `uk_<field>` / `idx_<field>` 重命名
  （第三轮后 `ix_` × 42、`idx_` × 1、`uq_` × 4；原 58 个为 `ix_` × 51、`idx_` × 2、`uq_` × 5）；
- 1 个未命名 `UniqueConstraint`（`managed_subagents.name`）必须补名；
- 名称长度全部 ≤ 100 字符。**第三轮后最长的是**
  `idx_personal_access_tokens_token_digest`（39 字符）——
  原最长项 `uq_channel_connection_owner_provider_identity`（45 字符）
  **随渠道删除消失**，改名后的名称长度压力进一步下降。

### 7.6 Query 规范（§6）

§3.10 已列出 6 类违规。迁移到 MySQL 后**不得**用
`JSON_UNQUOTE(JSON_EXTRACT(...))` 或 `REGEXP` 复刻这些查询。
正确做法是把过滤下沉到应用层，或按 §7.1 方案 B 拆列。

### 7.7 Migration 规范（§7）

**当前状态与本任务规范的映射**

| 任务规范 | 本项目现状 | 落地方式 |
| --- | --- | --- |
| 已提交 SQL schema 视为 immutable | 项目**没有** SQL schema 文件；等价物是 **24 个 Alembic Python revision** `0001_baseline` … `0023_user_preferences`（注意有两个 `0019_*`：`0019_projects` 与 `0019_thread_incarnations`） | **把 0001–0023 视为 immutable**，不修改 |
| Docker initialization scripts immutable | 项目**没有** DB init 脚本；Schema 由 `persistence/bootstrap.py:603-701` 的三分支状态机在启动期生成 | MySQL 侧需新增一个 **`mysql` 分支**（`_bootstrap_mysql`），**不改动**现有 PG / SQLite 分支（§7.8） |
| 新 Schema 修改必须创建 `00N_xxx.sql` | 现有约定是 `00NN_<name>.py`（Alembic Python revision） | **见下方"规范冲突 2"** —— 第四轮后**仅剩这一处偏差** |
| Fresh Instance：original → 001 → 002 → latest | MySQL 侧等价：**`0001_mysql_baseline`（MySQL 链根，含裁剪后的 15 张表）→ `0002_mysql_*` → … → head** | ✅ **第四轮后完全满足**：MySQL 有**自己的** `0001`，顺序执行**自己的**链，不碰 PG 的 `0001`–`0023` |
| Existing Instance：DBA 逐条执行每个新 migration | 等价：`alembic upgrade head`，由 `bootstrap_schema` 驱动 | 需要为 MySQL 提供显式 DBA 执行清单（§19） |
| 严格按数字顺序执行 | Alembic 自身保证链式顺序 | 保持 |
| 不得修改历史 SQL | 现有 `0021_batch_acceptance.py` / `0013_*` / `0002_*` 使用 `sa.JSON()` | 这些**不改**；MySQL baseline 在新 revision 中重建表结构 |

**✅ 原"规范冲突 1"：MySQL 无法重放 0001–0023 —— 第四轮已由独立链解决，不再构成偏差**

（以下保留原分析作为**背景记录**，因为它是"为什么必须独立链"的直接证据。）

`0018_oauth_identity_pg_partial.py:50,68` 直接查询 **PostgreSQL 系统目录**：

```sql
SELECT indpred FROM pg_index WHERE indexrelid = to_regclass('idx_users_oauth_identity')
```

`upgrade()` 与 `downgrade()` 都有 `if bind.dialect.name != "postgresql": return` 的短路保护
（`:56-57`、`:66-67`），因此在 MySQL 上执行**不会报错**。但这恰恰是问题所在：

- 这条 revision 的**语义本身是 PG 专有的**（它修补的是"0001 建索引时漏了 `postgresql_where`"这个
  PG 侧缺陷，见 `:31-41` 的说明）。在 MySQL 上它是个 **no-op**，
  于是"重放 0001–0023"在 MySQL 上产生的最终状态**与 ORM 模型不一致**
  （`users` 的 OAuth 唯一索引会停留在 0001 的形态）。
- 更根本的是：把一条只在 PG 上有意义的 DDL 当作"原始脚本"要求 MySQL 重放，
  在语义上不成立 —— 它既不是幂等的（对 MySQL 而言是空操作），也不是"不可变历史"的合理对象。

→ **第四轮的处置（不再是"偏差"，而是"绕开了"）**：
MySQL **从不重放** `0001`–`0023`。MySQL 有**自己的链根** `0001_mysql_baseline`（§7.8），
它由**裁剪后的最终 Schema** 生成。因此：

| 原来的问题 | 第四轮后 |
| --- | --- |
| "MySQL 重放 `0018` 会读到 PG 系统目录" | ✅ **不存在** —— MySQL 的链里根本没有 `0018` |
| "重放后最终状态与 ORM 不一致" | ✅ **不存在** —— baseline 本身就是 ORM 的目标形态 |
| "需要用户确认的第一号规范偏差" | ✅ **作废** —— 该偏差是**由构造消除**的，不是"被接受"的 |

> ⚠️ 但要说清一处仍然存在的约束：`0018` 这类 revision 在 **PG 链上依然存在且 immutable**，
> 所以只要 PG backend 还在（整个迁移期），它就必须**继续可执行**。
> 它的退役时点是**阶段 6**（随 PostgreSQL backend 一并删除）。

**🔴 规范冲突（第四轮后仅剩这一处）：migration 载体是 Alembic Python revision，不是裸 SQL**

任务要求 `001_xxx.sql` / `002_xxx.sql` 形式的 numbered SQL migration。
本项目已确立 Alembic Python revision 为唯一载体，且
`bootstrap.py` 的整个状态机（`_get_head_revision` / `_get_known_revisions` /
`ScriptDirectory` / `_get_alembic_config` / `_stamp` / `_upgrade`）都依赖 Alembic 脚本树，
另外 `migrations/_helpers.py` 的 `safe_add_column` / `safe_drop_column` 幂等 helper
与 `migrations/env.py` 的 `render_as_batch=True`（SQLite ALTER 支持）也都是 Alembic 特性。

若强制改成裸 SQL 文件，需要**重写整个 bootstrap 层**。

→ **建议保持 Alembic revision 形式**，MySQL 侧**另起一条链，编号从 `0001_mysql_baseline` 起**
（第四轮；原方案是"从 0024 续"，已取消）。
这是**第四轮后唯一保留的规范偏差**（§20 Q1）。

**✅ 一处已有先例（第四轮成为核心机制）**：`migrations/AGENTS.md:128` 已经确立了
`version_table="<prefix>alembic_version"` 的用法（扩展使用独立 Alembic 链时），
因此 `ag_alembic_version` 的改名**有现成模式可循**。
**第四轮的 MySQL 独立链正是沿用这一模式**，见 §7.8。

---

### 7.8 MySQL 独立 migration chain 与 Fresh Bootstrap 设计（第四轮新增）

#### 7.8.1 为什么必须"独立链"，而不是"PG 链 + 新 root"

| 方案 | 问题 |
| --- | --- |
| A. 单链 + `0024` 新 root（**原方案，第四轮取消**） | 一条 Alembic 链**只能有一个 root**。`0001_baseline` 已是 root，再加一个 root ⇒ **multiple heads**；`ScriptDirectory.get_current_head()` 在多 head 时**抛 `CommandError`**，而 `bootstrap.py::_get_head_revision()` 会把它包成 `RuntimeError` ⇒ **启动期直接失败**。要规避只能插"假的链边 revision"，属于自造机制。 |
| B. **独立链（第四轮采用）** | 两条链各有自己的 `script_location` 与 `version_table`，`alembic heads` 在**各自链内**只有一个 head。**已有先例**（`migrations/AGENTS.md:128`），无需新机制。 |

> 换句话说：`0024` 方案要解决的是"**如何在一个链里塞两个起点**"，
> 而独立链让这个问题**不存在**。这是本轮最根本的一处设计收敛。

#### 7.8.2 双链隔离的接线点（逐处核对）

| 接线点 | 现状（PG 单链） | MySQL 侧需要的改动 |
| --- | --- | --- |
| `script_location` | `_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"` —— **单目录** | MySQL 需要**第二个脚本目录**（如 `persistence/migrations_mysql/`），或在同目录下按子目录隔离 |
| `version_table` | **未设置** ⇒ 默认 `alembic_version` | MySQL 链显式设 **`ag_alembic_version`**（同时满足 §7.2 的 `ag_` 前缀规范与 Q9 决策） |
| head / known-revision 缓存 | `_HEAD_REVISION` / `_KNOWN_REVISIONS` 是**模块级单例**；`_get_head_revision()` / `_get_known_revisions()` 都只认**一个** `script_location` | 🔴 **必须按链做键**（`dict[chain, ...]`）。否则两条链**互相污染 head 判定** —— 这是独立链最容易踩的坑 |
| `_get_head_revision()` 的失败语义 | `get_current_head()` 返回 `None` 时抛 `RuntimeError("alembic has no head revision -- versions/ directory is empty")` | 语义保持，但必须确保它读的是**本链**的 head |
| `_get_alembic_config(engine, *, postgres_schema="")` | 注入 `script_location` + `sqlalchemy.url` + 可选 `deerflow_pg_schema` | MySQL 分支注入**本链** `script_location` + `version_table`；**不带** `deerflow_pg_schema` |
| `migrations/env.py` | 读 `deerflow_pg_schema`、调 `build_asyncpg_connect_args`、`render_as_batch=True`（注释明写 "Required for SQLite ALTER TABLE support"）、SQLite `PRAGMA busy_timeout` 钩子 | **这些对 MySQL 全都不适用**。MySQL 链需要自己的 `env.py`（或按方言分支），**不复用 PG 的注入逻辑** |
| `_read_database_revision(conn)` | `SELECT version_num FROM alembic_version`，且要求**恰好一行** | 改为读 `ag_alembic_version`；"恰好一行"的断言保留（它是防串链的有效检查） |

#### 7.8.3 `bootstrap_schema` 的 `mysql` 分支

现状：`bootstrap_schema(engine, *, backend, postgres_schema="")` 内部
`_bootstrap_lock(engine, *, backend)` 对未知 backend **抛 `ValueError`** ⇒ MySQL 必须显式加分支。

**MySQL 的状态机可以比 PG 简单得多**（这是"独立链 + 无历史数据"带来的直接收益）：

| 状态 | PG 现状 | **MySQL 设计** |
| --- | --- | --- |
| `empty`（无版本表、无 deerflow 表） | `create_all` → `stamp head` | **直接 `upgrade(head)`** —— 从 `0001_mysql_baseline` 顺序执行到 head。**不需要 `create_all`**，因为链根本身就是完整建表 |
| `versioned`（有版本表） | `upgrade(head)` | `upgrade(head)`（同） |
| `legacy`（有 deerflow 表但**无**版本表） | `_run_baseline_create_all_sync` + `stamp 0001_baseline` | 🔴 **直接拒绝启动（fail closed）** —— MySQL 不存在"Alembic 之前就存在"的部署，出现该状态只可能是**连错库或人为改库** |
| `forward-compatible`（`0018` → `0019` 滚动前向） | `_FORWARD_COMPATIBLE_REVISION = "0019_thread_incarnations"` + `_validate_forward_schema()` | ✅ **不需要** —— 该机制是为 PG 那条滚动前向兼容链（`0018 → 0019_projects → 0020 → 0021 → 0019_thread_incarnations → 0022 → 0023`）而存在；MySQL 链是**从 `0001` 起的线性链**，不存在滚动前向 |

> 🔴 **这是第四轮最有价值的一处简化**：MySQL 的 bootstrap **不需要** `create_all`、
> **不需要** schema-floor 校验、**不需要** forward-compatible 分支、**不需要** legacy 分支。
> 它退化为"**没有版本表就 upgrade，有版本表也 upgrade**" —— 实质上**只剩 `upgrade(head)` 一条路径**。

#### 7.8.4 legacy 清单：只服务旧 PostgreSQL bootstrap 的逻辑

| 符号 | 位置 | 为什么是 legacy | 退役时点 |
| --- | --- | --- | --- |
| `_CANONICAL_0019_SCHEMA_FLOOR` | `bootstrap.py:134-171` | 硬编码 `0019` 规范 schema 的"表 → 列"地板，供 `_validate_forward_schema()` 使用；**MySQL 链没有 `0019`，也没有滚动前向** | 阶段 6 |
| `_BASELINE_TABLE_NAMES` | `:198-210` | 供 `_run_baseline_create_all_sync()` 在 **legacy 库**上重建 `0001` 的产物；MySQL 无 legacy 分支 | 阶段 6 |
| `_BASELINE_INDEX_NAMES` | `:215-249` | 同上（`create_all` 不建索引，需显式补建） | 阶段 6 |
| `_BASELINE_REVISION = "0001_baseline"` | 模块级 | legacy 分支 `stamp` 的目标 revision（**PG 的 `0001`，不是 MySQL 的 `0001`**） | 阶段 6 |
| `_FORWARD_COMPATIBLE_REVISION` | 模块级 | 只为 PG 滚动前向 | 阶段 6 |
| `_validate_forward_schema()` | — | 依赖 `_CANONICAL_0019_SCHEMA_FLOOR` | 阶段 6 |
| `_run_baseline_create_all_sync()` | — | legacy 分支专用 | 阶段 6 |
| `_PG_LOCK_KEY` / `_postgres_lock()` | — | PG 专属 | 阶段 6 |
| 两个反向 pin 测试 | `test_baseline_table_names_constant_matches_0001` / `..._index_names_...` | 只 pin **PG 链**的 `0001_baseline`；MySQL 侧**不改它们**，随 PG backend 一起退役 | 阶段 6 |
| `_run_create_all_sync()` | — | ⚠️ **不删** —— SQLite / 开发路径仍在用；只是 **MySQL 不用它** | 保留 |

> ⚠️ **一个必须避免的误操作**：不要为了"MySQL 用得上"而**改写**这些常量的内容
> （例如把 5 张渠道表从 `_CANONICAL_0019_SCHEMA_FLOOR` 里删掉）。
> 它们必须与 PG 的 `0001_baseline` 保持**逐字一致**，否则两个 pin 测试失败、
> PG 侧的 bootstrap 也会失去保护。
> **正确做法是"一个字都不动"，让它们随 PG backend 一起退役** ——
> 这也正好回答了第三轮遗留的那个"drop migration + pin 测试语义重定义"的难点：
> **它随 `0024` 方案的取消而一并消失**。

#### 7.8.5 bootstrap 的并发保护 —— **第五轮定稿：不做 DB 锁**（单一 Migrator）

> ✅ **第五轮决策：不为 MySQL bootstrap 实现任何数据库分布式锁。**
> 所有 MySQL 实例都是**空库 fresh cutover**，运维约定为
> **"先由单一 Migrator 执行 migration，然后再启动多实例"**（§20.7 的原结论）。
> ⇒ `_postgres_lock()` / `_PG_LOCK_KEY` **随 PostgreSQL backend 在阶段 6 退役，不迁入 MySQL**。
> ⇒ 原 V1 的"并发保护二选一"子问题**就此关闭**（§20.11）。
>
> **收益**：省掉一套锁的生命周期管理（连接持有、超时、异常释放、连接池交互），
> 也避免了"锁没释放导致启动卡死"这一类运维故障。

（以下保留原辨析作为**记录** —— 它说明"为什么当初会考虑 `GET_LOCK`"，
以及"如果未来真需要加锁，正确的做法是什么"。）

`_postgres_lock()` 用 `pg_advisory_lock(_PG_LOCK_KEY)` 把 bootstrap 串行化。
MySQL 的等价物是 `GET_LOCK`，而 §20.7 明确**拒绝** `GET_LOCK()` ——
理由是"**connection-scoped**，连接归还池时锁不释放"。

🔴 **但那个理由在本场景不成立**，必须区分两类用法：

| | 业务事务锁（§20.7 拒绝 `GET_LOCK` 的场景） | **bootstrap 锁** |
| --- | --- | --- |
| 临界区边界 | 一个**短事务**；事务结束后连接**立刻归还池** | **整个 bootstrap 过程**；连接**被持有到结束** |
| `GET_LOCK` 是否匹配 | ❌ 锁存活期与事务不匹配 ⇒ 锁泄漏 | ✅ 锁存活期 == 连接持有期 == 临界区 ⇒ **语义正好匹配** |

> 因此**如果未来真需要加锁**，正确做法是
> `SELECT GET_LOCK('<name>', <timeout>)` + `RELEASE_LOCK('<name>')`，
> 并**显式在同一连接上持有到 bootstrap 结束** —— 而不是"`GET_LOCK` 一律不能用"。
> **但本阶段不需要它**：单一 Migrator 已经足够。

#### 7.8.6 MySQL migration 验收标准（第四轮重定义）

1. **空 MySQL 8.0.24 实例**（无任何表、无 `ag_alembic_version`）；
2. 从 **`0001_mysql_baseline`** 开始，**顺序执行** MySQL 后续 revision；
3. 最终 **只有一个 MySQL head**（在 MySQL 链上 `alembic heads` 返回单值）；
4. **Fresh bootstrap 不依赖任何 PostgreSQL migration，也不含任何 PostgreSQL-specific SQL**
   —— 无 `JSONB` / `BYTEA` / `pg_index` / `to_regclass` / `CREATE INDEX CONCURRENTLY` /
   advisory lock / partial index / `RETURNING`；
5. 执行完毕后的实际 schema **与裁剪后的 ORM 模型一致**（**15 张应用表 / 224 列**），
   且**不含任何渠道表**；
6. **重复执行幂等**（`versioned` 状态下 `upgrade(head)` 无副作用）；
7. **与 PG 链互不影响**：MySQL 链的 `script_location` / `version_table` / head 缓存
   均不读写 PG 链的任何状态。

> 验收 4 与 7 是第四轮**新增的硬性条款** ——
> 它们把"独立链"从设计意图变成**可检查的事实**。

---

## 8. LangGraph Checkpoint Analysis

### 8.1 版本与实现确认（实测，非声明）

| 项 | 值 | 证据 |
| --- | --- | --- |
| `langgraph` | **1.2.9** | `.venv` `importlib.metadata` 实测 |
| `langgraph-checkpoint` | **4.1.1** | 同上 |
| `langgraph-checkpoint-postgres` | **3.1.1**（**已安装**） | 同上（第一版误报为未安装） |
| `langgraph-checkpoint-sqlite` | **3.1.1** | 同上 |
| `langgraph-api` | **0.10.0** | 同上 |
| 官方可用的 checkpoint 后端 | `base`、`memory`、`postgres`、`serde`、`sqlite` | `ls .venv/.../langgraph/checkpoint/` |
| 官方可用的 store 后端 | `base`、`memory`、`postgres`、`sqlite` | `ls .venv/.../langgraph/store/` |
| **MySQL 后端** | **不存在** | `grep -rli mysql .venv/.../langgraph/` → **零命中** |

**当前 Saver**

| 路径 | Saver |
| --- | --- |
| 异步（Gateway 主路径） | `langgraph.checkpoint.postgres.aio.AsyncPostgresSaver`，连接来自 `psycopg_pool.AsyncConnectionPool`（`runtime/checkpointer/async_provider.py:129-140`） |
| 同步（TUI / CLI / `DeerFlowClient`） | `langgraph.checkpoint.postgres.PostgresSaver`，`from_conn_string`（`runtime/checkpointer/provider.py:132-147`） |
| Delta 模式包装 | `CachedHistorySaver`（`runtime/checkpointer/cached_saver.py`），缓存后端 `MemoryCheckpointHistoryCache` 或 Redis（`runtime/checkpoint_cache/`） |

**sync / async**：两条路径都提供，共享同一个 `CheckpointerConfig`。
**`checkpoint_channel_mode`**：`full`（默认）或 `delta`；`delta` 使用 LangGraph `DeltaChannel`，
`messages` 通道由 `merge_message_writes` 折叠。

### 8.2 Checkpoint Schema（3.1.1 实测 DDL）

从 `.venv/.../langgraph/checkpoint/postgres/base.py` 逐行核验：

```sql
CREATE TABLE IF NOT EXISTS checkpoint_migrations (v INTEGER PRIMARY KEY);

CREATE TABLE IF NOT EXISTS checkpoints (            -- base.py:47-56
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    parent_checkpoint_id TEXT,
    type TEXT,
    checkpoint JSONB NOT NULL,                      -- :53
    metadata JSONB NOT NULL DEFAULT '{}',           -- :54
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);

CREATE TABLE IF NOT EXISTS checkpoint_blobs (       -- base.py:58-67
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL,
    version TEXT NOT NULL,
    type TEXT NOT NULL,
    blob BYTEA,                                     -- :63
    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
);

CREATE TABLE IF NOT EXISTS checkpoint_writes (      -- base.py:69-79
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    channel TEXT NOT NULL,
    type TEXT,
    blob BYTEA NOT NULL,                            -- :74
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);

ALTER TABLE checkpoint_blobs ALTER COLUMN blob DROP not null;
SELECT 1;                                   -- no-op 占位
CREATE INDEX CONCURRENTLY IF NOT EXISTS checkpoints_thread_id_idx      ON checkpoints(thread_id);      -- :82
CREATE INDEX CONCURRENTLY IF NOT EXISTS checkpoint_blobs_thread_id_idx ON checkpoint_blobs(thread_id); -- :85
CREATE INDEX CONCURRENTLY IF NOT EXISTS checkpoint_writes_thread_id_idx ON checkpoint_writes(thread_id); -- :88
ALTER TABLE checkpoint_writes ADD COLUMN IF NOT EXISTS task_path TEXT NOT NULL DEFAULT '';
```

**关键 SQL（PostgreSQL 专有）**

```sql
-- SELECT_SQL（base.py:102-108）：一次查询取出 checkpoint + channel_values + pending_writes
select thread_id, checkpoint, checkpoint_ns, checkpoint_id, parent_checkpoint_id, metadata,
  (select array_agg(array[bl.channel::bytea, bl.type::bytea, bl.blob])
   from jsonb_each_text(checkpoint -> 'channel_versions')     -- :103
   inner join checkpoint_blobs bl
     on bl.thread_id = checkpoints.thread_id
    and bl.checkpoint_ns = checkpoints.checkpoint_ns
    and bl.channel = jsonb_each_text.key                      -- :107
    and bl.version = jsonb_each_text.value) as channel_values -- :108
  ...
  (select array_agg(array[cw.task_id::text::bytea, cw.channel::bytea, cw.type::bytea, cw.blob]
                    order by cw.task_id, cw.idx)              -- :112
   from checkpoint_writes cw where …) as pending_writes
from checkpoints

-- SELECT_PENDING_SENDS_SQL（base.py:123）
select checkpoint_id,
       array_agg(array[type::bytea, blob] order by task_path, task_id, idx) as sends
from checkpoint_writes
where thread_id = %s and checkpoint_id = any(%s) and channel = '{TASKS}'
group by checkpoint_id

-- UPSERT_* 使用 ON CONFLICT (…) DO UPDATE / DO NOTHING + %s 占位符
-- DeltaChannel stage-1（base.py:202 起）：动态生成 2K 个并行 JSONB key lookup
--   checkpoint -> 'channel_versions' ->> %s AS ver_0,
--   (checkpoint -> 'channel_values' -> %s) IS NOT NULL AS hs_0, …
--   注释（:195-196）明确说这个设计"avoids JSONB serialization on the wire"
```

### 8.3 是否存在可用的 MySQL Checkpointer（严格分类）

| 类别 | 结论 |
| --- | --- |
| **LangGraph 官方支持** | ❌ **不存在**。官方仅提供 `langgraph-checkpoint`（core：base/memory/serde）、`langgraph-checkpoint-sqlite`、`langgraph-checkpoint-postgres`。实测 `langgraph/checkpoint/` 下只有 `base`、`memory`、`postgres`、`serde`、`sqlite`；`grep -rli mysql` 全树零命中 |
| **官方独立 package** | ❌ 无 `langgraph-checkpoint-mysql` 官方包 |
| **第三方实现** | ⚠️ 存在：`langgraph-checkpoint-mysql`（PyPI **3.0.0**，2026-01-23，作者 **Theodore Ni** `<dev@ted.bio>`，仓库 `github.com/tjni/langgraph-checkpoint-mysql`）。`requires_dist`：`langgraph-checkpoint>=2.1.2`、`orjson>=3.10.1`、`typing-extensions>=4.12.2`；可选 extra：`aiomysql` / `asyncmy` / `pymysql`。**它不是 LangChain/LangGraph 官方组件** |
| **当前项目已有实现** | ❌ 无。项目只做**包装**（`CachedHistorySaver`）与**配置选择**，没有自定义 saver |
| **需要自行实现** | ✅ **这是当前唯一的合规路径**（理由见 §16.2） |

### 8.4 第三方 MySQL Saver 的违规明细

`langgraph-checkpoint-mysql` 3.0.0 的 `langgraph/checkpoint/mysql/base.py`（`MIGRATIONS`）：

| 规范项 | 该包的实现 | 违规 |
| --- | --- | --- |
| 禁止 `JSON` | `checkpoints.checkpoint JSON NOT NULL`、`checkpoints.metadata JSON NOT NULL`、`store.value JSON NOT NULL` | ❌ |
| 禁止 `BLOB`/`LONGBLOB` | `checkpoint_blobs.blob LONGBLOB`、`checkpoint_writes.blob LONGBLOB NOT NULL` | ❌ |
| 禁止 specialized types | `checkpoint_ns_hash BINARY(16) AS (UNHEX(MD5(checkpoint_ns))) STORED` | ❌ |
| 表名 `ag_` 前缀 | `checkpoints` / `checkpoint_blobs` / `checkpoint_writes` / `checkpoint_migrations` / `store` / `store_migrations` | ❌ |
| 禁止 FK | 无 FK | ✅ |
| 至少一个 PK | 每表都有 | ✅ |
| 中文 COMMENT | 无任何 COMMENT | ❌ |
| 索引命名 `uk_`/`idx_` | `checkpoints_thread_id_idx` 等 | ❌ |
| 禁止数据库专有业务逻辑 | 大量 `json_table` / `json_keys` / `json_extract` / `json_unquote` / `json_arrayagg` / `json_contains` / `UNHEX(MD5())` / `to_base64` | ❌ |
| 官方支持声明 | **第三方**，非 LangChain 官方 | ⚠️ 必须明确标注 |

**判定**：该包在**功能上**可以让 LangGraph 跑在 MySQL 上，
但在**本项目规范下不可用**。且因为它的核心 SQL 依赖 JSON 函数
（用于从 `checkpoints.checkpoint` 里提取 `channel_versions`），
"只改列类型"的 fork 会导致 SQL 全面重写 —— **性价比低于自研**。

### 8.5 Checkpoint 迁移难度判定：**Hard**（接近 Replace）

理由：

1. 序列化格式是 **msgpack（主）/ pickle（回退）**，不是 JSON（§12.1）。
2. `checkpoint` / `metadata` 两列是 JSONB，且 `checkpoint` 的**内部结构**
   （`channel_versions`、`channel_values`、`versions_seen`）被 SQL 直接查询。
3. `CREATE INDEX CONCURRENTLY` 语法在 MySQL 上直接报错。
4. `TEXT` 主键在 MySQL 上不允许 → 4 张表的主键列全部要收紧为 `VARCHAR(N)`，
   且必须**实测** `checkpoint_ns`（嵌套子图会增长）的真实最大长度
   （**唯一保留的待实测项之一**，§20.9 / §20.11 V2；`VARCHAR(255)` 仅为占位）。
5. 并发写语义：checkpointer 靠主键冲突吸收并发写（blobs 用 `DO NOTHING`、
   checkpoints/writes 用 `DO UPDATE`）。MySQL 的 `INSERT IGNORE` 会**吞掉所有错误**
   （包括数据截断），必须改用 `ON DUPLICATE KEY UPDATE col = col`（no-op 更新）。
6. `%s` 占位符需要整体改为 `%s`（MySQL 驱动同样接受 `%s`，但 psycopg 的
   `Jsonb` 包装、`dict_row` row_factory、`prepare_threshold` 等都要替换）。

### 8.6 sync / async 双路径的重新评估（第五轮新增）

> **决策：第一阶段只实现 async CheckpointSaver，不实现同步 MySQL Saver。**
> 但**同步数据库驱动必须保留** —— 理由见下（这是一个容易做错的区分）。

#### （1）现状

| 路径 | 实现 | 位置 |
| --- | --- | --- |
| 异步（Gateway 主路径） | `AsyncPostgresSaver` + `psycopg_pool.AsyncConnectionPool` | `runtime/checkpointer/async_provider.py:129-140` |
| 同步 | `PostgresSaver` + `from_conn_string` | `runtime/checkpointer/provider.py:132-147` |
| 同步工厂入口 | `get_checkpointer()` / `checkpointer_context()` / `_sync_checkpointer_cm()`（**全部 `@contextlib.contextmanager` + `threading.Lock`**） | `runtime/checkpointer/provider.py:196` / `:268` / `:103` / `:154` |

#### （2）同步路径的消费者（完整清单，逐条分类）

| 消费者 | 位置 | 分类 | 是否生产 |
| --- | --- | --- | --- |
| **`DeerFlowClient`** | `client.py:145`（定义）、`:473`/`:621`/`:920`（构造同步 checkpointer） | **同步 API**（`def stream` / `def chat`，**无 async 版本**） | ❌ **无任何生产引用** —— 全仓 grep `DeerFlowClient` / `from deerflow.client` 在 `app/` 与 `packages/harness/deerflow/` 内**零命中** |
| **TUI** | — | — | ❌ **代码已删除**：`tui/` 仅剩空的 `widgets/` 目录、无任何 `.py`；`pyproject.toml` 已无 `[project.scripts]` |
| CLI | `extensions/cli.py`、`skills/review/cli.py`、`scripts/benchmark/deermem_eviction/cli.py` | — | ❌ 均**不构造 checkpointer** |
| 示例脚本 | `scripts/e2e_safety_termination_demo.py:107` | 脚本 | ❌ 非生产 |
| 配置热重载 | `config/app_config.py:503-506` `reset_checkpointer()` | 单例重置 | ⚠️ 只重置，**不建 saver** |
| health | `app/gateway/health.py:115` `_resolve_checkpointer_config` | **仅配置解析** | ✅ 生产，但**不建 saver** |
| 测试 | `test_checkpointer.py`、`test_client*.py`、`test_app_config_reload.py` 等 | 测试 | — |

> **结论**：同步 **Saver 的构造路径**在 `app/` 与 `packages/` 内
> **唯一的生产消费者是 `DeerFlowClient`**，而它在 TUI 删除后已无入口、退化为调试用途。
> ⇒ **不需要为它实现同步 MySQL Saver。**

#### （3）🔴 但必须区分"同步 Saver"与"同步驱动" —— 后者**不能删**

| | 同步 **Saver** | 同步**数据库驱动** |
| --- | --- | --- |
| 谁需要 | 只有 `DeerFlowClient`（调试） | **`agent_storage.backend: db`** —— `SqlAgentStore` |
| 证据 | `client.py:473,621,920` | `persistence/agents/__init__.py:56` `SqlAgentStore(config.database.app_sync_sqlalchemy_url)`；`persistence/agents/sql.py:52` `create_engine(...)`（**同步**）；`persistence/managed_subagents/__init__.py:36` 同理 |
| 调用方 | — | `routers/agents.py:265,352,468,541`、`routers/subagents.py`、`setup_agent_tool.py:61`、`update_agent_tool.py:227`、`config/agents_config.py:213,241,261`、`subagents/registry.py:66` |
| 关键约束 | — | `persistence/agents/__init__.py` 的 `get_agent_store` docstring 明写 **"the per-run agent build runs in the graph subprocess"** ⇒ **图子进程用同步方式构建 agent** |
| **第五轮处置** | ❌ **不实现** | ✅ **必须保留**（MySQL 侧需要同步驱动，如 `PyMySQL`） |

> 🔴 **这是本轮最容易做错的一处**：看到"同步路径没有生产消费者"就顺手把同步驱动也去掉，
> 会让 `agent_storage.backend: db` 整条链（agent / managed-subagent 定义的读写）**直接坏掉** ——
> 它与 checkpointer 无关，是**独立的同步驱动刚需**。
>
> 若希望**彻底消除**同步驱动，唯一路径是把 `SqlAgentStore` 改为 async；
> 那是一次独立重构（涉及图子进程的同步构建上下文），**不属于本次数据库替换的范围**。
> ⇒ 建议：**第一阶段保留同步驱动，且只为 `SqlAgentStore` 服务**。

#### （4）对 §20.3（驱动最小化）的修正

原结论是"异步主路径用 `asyncmy`；只有确认仍存在同步消费者时才加 `PyMySQL`"。
**第五轮的答案是：确实有，且只有一个**（`SqlAgentStore`）。
⇒ **`PyMySQL` 进入范围**（`mysql+pymysql://` 供同步 engine 使用），
但**不实现同步 Saver**（不引入任何第三方同步 MySQL saver）。

---

## 9. LangGraph Store —— **整体删除，不迁移**（第五轮改写）

> **本节性质已变更。** 第二～四版此节是"Store 使用面很窄，需要自研一个 MySQL Store"；
> **第五轮改为：不迁移 Store，直接删除。** 原"自研方案 / 第三方评估 / pgvector 讨论"全部作废。

### 9.1 五个"看起来像 Store"的概念必须分开（这是最容易混的一处）

| # | 名称 | 实现 / 位置 | 存储 | 依赖 PostgreSQL | 生产运行链 | **第五轮处置** |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | **LangGraph Store**（`BaseStore`） | `runtime/store/{provider,async_provider,__init__,_sqlite_utils}.py` | 跟 `checkpointer:` / `database:` 走 | ✅（`AsyncPostgresStore`） | ❌ **只构造、不读写**（DB 模式） | 🔴 **删除** |
| 2 | **RunStore** | `runtime/runs/store/{base,memory}.py` + `persistence/run/sql.py` | `runs` 表 | ✅ | ✅ 是 | ✅ **保留并迁移** |
| 3 | **ThreadMetaStore** | `persistence/thread_meta/{base,memory,sql,model}.py` | `threads_meta` 表（DB 模式）/ `MemoryThreadMetaStore`（memory 模式，**内部用 `BaseStore`**） | ✅ | ✅ 是 | ✅ **保留并迁移**（⚠️ 见 §9.3 第 6 项） |
| 4 | **Agent Memory** | `agents/memory/`（`deermem` / `mem0` / `honcho` / `openviking` / `noop`） | **本地文件 + SQLite FTS5** | ❌ **完全无关** | ✅ 是 | ✅ **保留，本来就不动** |
| 5 | **Agent definition persistence** | `persistence/agents/{base,file,sql,model}.py` + `persistence/managed_subagents/` | `agents` / `managed_subagents` 表（db 模式）或文件 | ✅（db 模式走**同步** SQLAlchemy） | ✅ 是 | ✅ **保留并迁移** |

### 9.2 🔴 一处必须先纠正的事实（第五轮调查发现）

文档此前（本节原文）称：

> "Store 只承载 `agents` 命名空间下的少量文档（agent 定义）"

**该表述是错误的。** 实测证据：

| 断言 | 实测结论 |
| --- | --- |
| `setup_agent_tool` 从 LangGraph Store 读 agent 定义 | ❌ **否**。`tools/builtins/setup_agent_tool.py:9` `from deerflow.persistence.agents import get_agent_store`、`:61` `store = get_agent_store()`、`:73` `store.update(...)` —— 用的是**自研 `AgentStore`**（file/db），与 `BaseStore` 无关。同源调用还有 `update_agent_tool.py:227`、`routers/agents.py:468`、`config/agents_config.py:213,241,261` |
| Store 里存在 `agents` 命名空间 | ❌ **未找到任何写入点**。全仓对 `BaseStore` 的 `aput/aget/asearch/adelete` **生产调用只有两处**，namespace 都是 `("threads",)` |
| Store 实际承载什么 | ✅ **memory 模式下的 `("threads",)` 线程元数据**：`persistence/thread_meta/memory.py:21` `THREADS_NS = ("threads",)`；`:38/68/157` `aget`、`:84/89/183` `aput`、`:135 asearch`、`:220 adelete` |
| DB 模式下 Store 被谁读写 | ⚠️ **只被"构造 + 挂载"，不被读写**：`deps.py:435` **无条件** `make_store(config)`；`worker.py:1257` `agent.store = store`、`checkpoint_state.py:129` `graph.store = store`、`services.py:1195` `store=ctx.store` 均**只挂载**；`deps.py:498` `make_thread_store(sf, app.state.store)` 因 `sf is not None` 而**选中 SQL 实现**，`store` 参数被忽略（`thread_meta/__init__.py:36-47`） |
| 唯一真正读 Store 的生产路径 | `app/gateway/app.py:129-171` 的**孤儿线程迁移**（`asearch(("threads",))` + `aput`）—— 它是 **"无鉴权 → 有鉴权"的历史升级路径**，且**非致命**（异常仅记日志） |
| 是否有 vector / semantic / namespace 树 | ❌ 无。`runtime/store/` 内 grep `index=` / `embedding` / `vector` / `semantic` → **0 命中**；`search()` 的两处调用**都不带 `query`** |

> **结论**：在 `database.backend: postgres`（⇒ MySQL）下，LangGraph Store 是一份
> **"构造了但没有任何生产读写"** 的设施；它唯一的真实用途是 **memory 模式的线程元数据兜底**。
> ⇒ **删除它比原先假设的更安全** —— 原先的顾虑（"agent 定义在里面"）**并不存在**。

### 9.3 删除 Store 的完整改动清单（**3 处非显然耦合必须一起处理**）

| # | 改动 | 位置 | 备注 |
| --- | --- | --- | --- |
| 1 | 删除 `runtime/store/{__init__,provider,async_provider}.py` | `runtime/store/` | |
| 2 | 🔴 **`_sqlite_utils.py` 必须保留 / 迁移，不能随目录删** | `runtime/store/_sqlite_utils.py` | `resolve_sqlite_conn_str` / `ensure_sqlite_parent_dir` 被 **`checkpointer/provider.py:33`、`checkpointer/async_provider.py:34`、`app/gateway/health.py:176`** 复用 ⇒ 移到 `runtime/checkpointer/` 或 `runtime/sqlite_utils.py` |
| 3 | 删除 `deps.py:379` 的 import 与 `:435` 的 `app.state.store = ...make_store(config)` | `app/gateway/deps.py` | 它是**无条件构造** —— 删掉即彻底消除"要不要写 MySQL Store"这个问题 |
| 4 | 删除 `store=` 管线（**5 处**） | `deps.py:716` `RunContext(store=...)`、`worker.py:806/1257`、`checkpoint_state.py:129`、`services.py:1195` | 纯挂载，无读取 |
| 5 | 🔴 **`make_thread_store(sf, app.state.store)` → `make_thread_store(sf)`** | `deps.py:498`、`persistence/thread_meta/__init__.py:36-47` | 该工厂的 `store` 参数**只为 memory 模式服务** |
| 6 | 🔴 **`MemoryThreadMetaStore` 的去向必须显式决定** | `persistence/thread_meta/memory.py` | 它需要一个 `BaseStore`。两个选择：**(a) 一并删除**，`make_thread_store` 在无 `sf` 时**直接抛错**（**推荐** —— memory 模式不是生产目标）；**(b) 改为独立 dict 实现**。⚠️ **`test_checkpointer.py` 等多处测试依赖 memory 模式**，选 (a) 需同步调整测试 |
| 7 | 删除孤儿线程迁移 | `app/gateway/app.py:129-171`（`_iter_store_items` / `_migrate_orphaned_threads`） | "无鉴权→有鉴权"的历史升级路径；**fresh cutover 没有孤儿数据**，该路径无意义 |
| 8 | 删除 `reset_store()` 调用 | `config/app_config.py:504,507` | 配置热重载时重置 Store 单例 |
| 9 | 删除 re-export | `runtime/__init__.py:12` | |
| 10 | `health.py` 去掉 Store 探针 | `app/gateway/health.py` | 保留 `_sqlite_utils` 的用法 |
| 11 | 测试调整 | `test_pg_schema_integration.py:16-17`、`test_app_config_reload.py:25`、`test_checkpointer.py`（多处）、`blocking_io/test_gate_smoke.py:28,49` | |

> ✅ **删除后**不再需要的东西（第五轮直接消除的工作量）：
> MySQL Store 自研、第三方 MySQL Store 评估、`ag_store` 表、Store migration、
> Store health / config / provider 迁移、Store 的 pgvector / vector 讨论，
> 以及 `CheckpointerConfig.postgres_schema` 里"legacy checkpointer/**store** tables"的语义。

### 9.4 删除后必须确认的三条不变量

1. **Gateway 启动不依赖 Store** —— `deps.py:435` 移除后，启动期只剩 checkpointer + engine + repositories。
2. **Agent 主运行链不依赖 Store** —— 三处 `graph.store = store` / `agent.store = store` 是挂载。
   ⚠️ **实施时必须回归验证**：确认**没有任何 graph node / tool 通过 LangGraph 运行时注入读 `store`**
   （本轮已 grep：`agents/` 与 `app/` 内**无** `BaseStore` 读写点）。
3. **Agent 定义读取不受影响** —— 走 `get_agent_store()`（`agents` 表 / 文件），与 Store 无关（§9.2）。

### 9.5 pgvector / Vector Search —— 结论（本项随 Store 删除而消失）

- **pgvector 未启用**：`runtime/store/async_provider.py` 只传 `conn_string`，**从不传 `index`**，
  因此 `PostgresStore` 的 `VECTOR_MIGRATIONS` 从未执行，`CREATE EXTENSION vector` 从未运行。
- **Store 删除后本项彻底消失** —— 不再需要任何 MySQL Vector 讨论（与 §11.2 同结论）。

---

## 10. Queue / Scheduler Analysis

### 10.1 五个调度/队列子系统

| 子系统 | 表 | 代码 | 关键并发原语 |
| --- | --- | --- | --- |
| **Scheduler**（定时任务） | `scheduled_tasks`、`scheduled_task_runs` | `persistence/scheduled_tasks/sql.py`、`persistence/scheduled_task_runs/sql.py`、`deerflow/scheduler/` | `pg_advisory_xact_lock`（全局预算，`sql.py:317`）、`with_for_update(skip_locked=True)`（due-task claim，`scheduled_tasks/sql.py:332,530`）、`UPDATE … RETURNING`（`occurrence_seq`，`scheduled_task_runs/sql.py:211`）、partial unique `uq_scheduled_task_run_active`、`FOR UPDATE` 行锁（固定 task → occurrence 顺序） |
| **MCP Tasks**（远程 MCP 任务轮询） | `mcp_tasks` | `persistence/mcp_tasks/sql.py`、`app/mcp_tasks/` | `with_for_update(skip_locked=True)`（poll/cancel/notification claim，`:230,376,480`）、`with_for_update(read=True)`（`:168`，**依赖 PG 四档行锁模型**）、lease + `dispatch_version` 乐观栅栏 |
| **Subagent Batches**（原生子 Agent 批次） | `subagent_batches`、`subagent_batch_items` | `persistence/subagent_batches/sql.py` | `with_for_update(skip_locked=True)` × **14 处**、`idx_subagent_batch_items_claim(status, lease_expires_at, batch_id)` |
| **Run ownership**（多 Worker 运行所有权） | `runs` | `persistence/run/sql.py`、`runtime/runs/manager.py`、`runtime/runs/worker.py` | `with_for_update()`、`claim_for_takeover`、**3 处 `RETURNING`**（`:563,594,627`）、`uq_runs_thread_active` partial unique、`IntegrityError` 判定 |
| ~~**Inbound dedupe**（跨 Pod webhook 去重）~~ | ~~`webhook_deliveries`~~ | ⛔ `app/channels/dedupe_store.py` 随渠道删除 | ~~`INSERT … ON CONFLICT … WHERE … RETURNING` + `now()` + `make_interval`~~ |

> **第三轮效果**：原"五个子系统"中，**Inbound dedupe 整体消失**（表 + 代码 + SQL 全部删除）。
> 剩下的四个（Scheduler / MCP Tasks / Subagent Batches / Run ownership）
> 是真正的**核心并发原语所在地**，也是迁移难度的全部来源 ——
> 它们**一个都没减少**（§1.6）。

### 10.2 迁移到 MySQL 的逐项行为变化

| # | 现语义 | MySQL 8.0.24 | 变化与影响 | 难度 |
| --- | --- | --- | --- | --- |
| 1 | `pg_advisory_xact_lock(key)` 事务级互斥，commit/rollback 自动释放 | **无等价物**。`GET_LOCK(name,t)` 是**连接级**，连接池归还时锁不释放 → 可能永久死锁 | 调度器全局预算锁必须重设计。**已决策（§20.7）：不使用 `GET_LOCK`**，采用 (a) 锁表 sentinel row + `SELECT … FOR UPDATE`；或 (b) "唯一约束 + 计数重试" | **Hard** |
| 2 | `pg_advisory_lock(key)` 会话级（bootstrap）+ `SET LOCAL idle_in_transaction_session_timeout = 0` | 两者都不存在 | bootstrap 跨进程互斥失去"DDL 期间保活"保护（MySQL 侧连接被 `wait_timeout` 杀掉会**静默释放**连接级锁）。**已决策（§20.7）：不实现数据库分布式锁**，改为"**单一 Migrator 串行执行 Alembic，然后启动多实例**" | **Hard** |
| 3 | `hashtext(thread_id)::bigint` 生成锁 key | MySQL 无 `hashtext`；`CRC32()` 只有 32 位 | 必须在**应用层**用 `sha256` 派生 64-bit key（`channel_connections/sql.py:401-404` 已有此模式可复用） | Easy |
| 4 | `UPDATE … SET x = x + 1 … RETURNING x` | **无 `RETURNING`** | `LAST_INSERT_ID(expr)` 后 `SELECT LAST_INSERT_ID()`；**但 `run_events.id` 是 AUTO_INCREMENT，同连接会互相污染**，需显式隔离或改两步法（`SELECT … FOR UPDATE` + `UPDATE`） | **Hard** |
| ~~5~~ | ~~`INSERT … ON CONFLICT … DO UPDATE … WHERE first_seen < TTL RETURNING channel`~~ | ⛔ **第三轮后本项消失** | ~~需重构为 `SELECT … FOR UPDATE` + 分支 + `INSERT`/`UPDATE`~~ —— `dedupe_store.py` 随渠道删除，MySQL 侧**不再需要处理这个最难的条件 upsert**。这是第三轮**唯一被消除的 Hard 项**，也是全表最高价值的缩减 | ~~**Hard**~~ → **已消除** |
| 6 | `make_interval(secs => :ttl)` | 无此函数 | `TIMESTAMPADD(SECOND, -:ttl, NOW())` 或 `DATE_SUB(NOW(), INTERVAL :ttl SECOND)` | Easy |
| 7 | `now()` | MySQL 有 `NOW()`，但它是**语句时间**而非事务开始时间 | `created_at <= created_before` 这类比较的边界在长事务里有细微差别 | Easy（需注意） |
| 8 | `uq_runs_thread_active` partial unique | 无 partial index | 生成列 workaround：<br>`active_thread_id VARCHAR(64) GENERATED ALWAYS AS (IF(status IN ('pending','running'), thread_id, NULL)) STORED` + `UNIQUE KEY uk_runs_thread_active (active_thread_id)`。MySQL 唯一索引允许多个 `NULL` → **精确等价** | Moderate |
| 9 | `uq_scheduled_task_run_active` partial unique | 同上 | 同 workaround（谓词 `status IN ('queued','launching','running')`） | Moderate |
| ~~10~~ | ~~`uq_channel_connection_active_identity` partial unique~~ | ⛔ **第三轮后本项消失** | ~~同 workaround（谓词 `status != 'revoked'`，3 列拼接生成列）~~ —— 表已删除 | ~~Moderate~~ → **已消除** |
| 11 | `idx_users_oauth_identity` partial unique | 同上 | 同 workaround。**额外风险**：`app/gateway/auth/repositories/sqlite.py:36-39` 依赖驱动错误里的约束名判定 OAuth 冲突；MySQL 1062 错误也带 key 名，但**错误对象形状不同**（`exc.orig` 是驱动原生异常而非 asyncpg 包装异常），判定逻辑必须改写 | Moderate |
| 12 | `with_for_update(skip_locked=True)` | ✅ 8.0.1+ 支持，实测编译为 `… LIMIT %s FOR UPDATE SKIP LOCKED` | 语义一致。**但**：无索引时会退化为全表扫描 + 锁全表；`SKIP LOCKED` 不能与 `NOWAIT` 同用 | Moderate |
| 13 | `with_for_update(read=True)`（`FOR SHARE`） | 实测编译为 **`LOCK IN SHARE MODE`**（MySQL 8.0 亦接受 `FOR SHARE`） | 语义相近。但 `mcp_tasks/sql.py:165-168` 的注释**明确依赖 PG 的四档行锁强度模型**（"FOR SHARE 会与 FOR NO KEY UPDATE 冲突，KEY SHARE 不会"）——**该论证在 MySQL 上不成立**，必须重新设计归属校验 | Moderate |
| 14 | 默认隔离级别 READ COMMITTED | MySQL/InnoDB 默认 **REPEATABLE READ** | **最大的隐性风险**：RR 下 `SELECT … FOR UPDATE` 与 `INSERT … ON DUPLICATE KEY UPDATE` 会加 **gap lock**，显著提高死锁概率。代码里有明确的锁顺序纪律（`scheduled_task_runs/sql.py:754-757`：task → scheduled-run；`scheduled_tasks/sql.py:680-684`），RR 下需重新验证 | **Hard** |
| 15 | `SELECT max(seq) … FOR UPDATE` 在 PG 报错，因此用 advisory lock | MySQL **允许该语句**（实测编译通过）；**但"允许执行"不等于"能串行化"** | ⚠️ **本版已修正（原判 Easy → Moderate）**。`runtime/events/store/db.py:153` 的 `else` 分支**不是通用实现，而是 SQLite 语义**：SQLite 忽略 `FOR UPDATE`，其正确性来自"库级单写者锁"，与 `FOR UPDATE` 无关；MySQL 静默落入该分支，因此**不能**据此删除 advisory-lock 分支。需实测 RC 下的真实锁范围与竞态窗口（**V3**，§13.7）。若不能保证串行化，改用 **per-thread counter/sentinel row + `SELECT … FOR UPDATE`**（与 §20.7 调度器预算锁同一模式），或**唯一约束 + bounded retry**（`uq_events_thread_seq` 已存在，但代码里目前**没有**任何重试） | **Moderate** |
| 16 | `INSERT IGNORE` vs `ON CONFLICT DO NOTHING` | MySQL `INSERT IGNORE` 吞掉**所有**错误（含截断/类型错误） | checkpointer 的 blobs upsert 若用 `INSERT IGNORE`，数据截断会静默丢失。必须改用 `INSERT … ON DUPLICATE KEY UPDATE col = col` | Moderate |
| 17 | `UPDATE … RETURNING` 经 SQLAlchemy | **SQLAlchemy 2.0.49 编译为含 `RETURNING` 的 SQL，且不报错**（实测） | 编译期/单测期静默通过，**运行期才 1064**。这是最容易漏的陷阱：现有 4 处 `.returning()` 必须**手工**改写，不能指望 ORM 报错 | **Hard** |
| 18 | **多实例 reconcile 被显式声明为 "Postgres-only"** | — | `scheduled_task_runs/sql.py:758` 的注释原文：**"Multi-instance reconciliation is Postgres-only."** 也就是说：调度器/运行所有权的**跨实例对账语义从来没有在 MySQL 上被定义过**。MySQL 不是"换一个后端"，而是要**新增第三条语义路径**（PG / SQLite / MySQL 三套并存）。这类"隐式只在 PG 上成立"的路径必须逐条挖掘，而不是等到迁移时才在测试里发现 | **Hard** |

### 10.3 两个必须显式验证的场景（本任务要求）

**场景 A：同一 `thread_id` 的两个 Agent Run 并发写 Checkpoint**

- PG 行为：`checkpoints` 主键 `(thread_id, checkpoint_ns, checkpoint_id)` 冲突 →
  `ON CONFLICT DO UPDATE`；`checkpoint_blobs` 冲突 → `DO NOTHING`。并发写**不报错**，后写覆盖。
- MySQL 行为：`ON DUPLICATE KEY UPDATE` 语义相近。
  **但**：RR 隔离下两个事务同时对同一 PK 做 upsert 会先取锁 → 后到者等待；
  若两者还持有其它行锁，可能死锁（MySQL 抛 `1213` 并 kill 一个事务）。
- **额外的语义变化**：现有保护是 `uq_runs_thread_active`（每线程至多一个活跃 run），
  因此 checkpoint 并发写的**正常路径是串行的**。该约束在 MySQL 上通过生成列唯一索引实现后
  **唯一性语义等价**，但错误码从 `23505` 变 `1062`，
  `runtime/runs/store/base.py` 的 `IntegrityError` 判定路径需要复核
  （`tests/test_multi_worker_postgres_gate.py` 是针对 PG 写的）。

**场景 B：两个 Worker 抢同一队列行（Scheduler / MCP Tasks / Subagent Batches）**

- PG：`SELECT … FOR UPDATE SKIP LOCKED` → 后到者跳过被锁行，取下一行。
- MySQL：`SKIP LOCKED` 支持，语义一致。
  **但**：RR 隔离下未命中的行会加 gap lock，导致"跳过"行为在间隙上不完全等价；
  且 `innodb_lock_wait_timeout` 默认 **50 秒**（PG 的 `lock_timeout` 默认无限）。
- 结论：**必须做并发压测**（`scripts/benchmark/concurrency/worker.py` 可作为起点）。

---

## 11. Knowledge / Vector Analysis

### 11.1 现状：项目**不使用** pgvector

| 检查项 | 结论 |
| --- | --- |
| `pgvector` / `vector` 扩展 | ❌ 未使用。`langgraph/store/postgres/base.py` 的 `VECTOR_MIGRATIONS` 只在传入 embeddings 配置时执行，DeerFlow 未传 |
| PostgreSQL 全文检索（`tsvector`/`to_tsquery`） | ❌ 未使用 |
| Embeddings / RAG 存储 | ❌ **不在数据库里**。RAG 通过 `deerflow/community/ragflow/` 以 **HTTP 调用外部 RAGFlow 服务** |
| 本地检索 | ✅ **SQLite FTS5**（`deermem/core/retrieval.py` 的 `CREATE VIRTUAL TABLE memory_fts USING fts5(...)`），BM25 排序，文件级存储 |
| 任务连续性归档检索 | ✅ SQLite FTS5（`agents/task_continuity/archive.py`），默认 `task_continuity.enabled: false` |
| 大 payload 卸载 | ✅ `tool_output.externalize_min_chars` → 写入 `.tool-results`；**HEAD 已改为经对象存储**（`object_storage/keys.py:33-37` 的 `tool_results_prefix`） |

### 11.2 结论：**不需要 MySQL 承担 Vector 职责**

应当**拆分**，而不是把 pgvector 强行换成 MySQL：

```
Agent Runtime
    ├── MySQL 8.0.24          ← Application Data / Checkpoint / Memory Metadata / Operational
    ├── Object Storage        ← Artifacts / Large Payload（HEAD 已落地 S3/MinIO 端口）
    ├── Knowledge Service     ← Vector / RAG（当前已是外部 RAGFlow HTTP）
    └── Remote Sandbox        ← 临时工作区
```

**重要**：这个拆分**不是迁移目标，而是现状的准确描述**。当前项目已经满足这个边界
（除了 Checkpoint 依赖 PG）。因此 "Knowledge / Vector" 在本次迁移中**难度为 Replace
（即：保持现状，不迁移）**，唯一动作是确认 `PostgresStore` 的向量迁移永远不被触发
（现状已满足）。

### 11.3 应该迁到 Object Storage 的数据（回答 §20 问题 11）

**HEAD 已经提供了实现端口**（`object_storage/port.py`：`write_stream` / `open_read` /
`stat` / `list_prefix` / `delete` / `copy`，aiobotocore 流式 multipart，
`keys.py` 定义了 `users/{user_id}/threads/{thread_id}/outputs|uploads|.tool-results`
与 `users/{user_id}/skills/custom` 的稳定命名空间），
因此下表从"需要新建基础设施"降级为"**需要把新数据类型接入已有端口**"。

| 数据 | 现状 | 建议 |
| --- | --- | --- |
| Tool 结果大文本 | ✅ 已卸载（阈值 12000 字符），**HEAD 已走对象存储** | 保持 |
| **Checkpoint 大 blob** | `checkpoint_blobs.blob` / `checkpoint_writes.blob` | **最高优先**：`messages` 通道是累积 reducer（`full` 模式下 blob 随会话长度单调增长），必然超出 `TEXT` 64KB。见 §12.4 |
| `mcp_tasks.result_artifact`（JSON） | DB 列，`max_result_bytes: 65536` | 超阈值时卸载，DB 只留引用 |
| `subagent_batch_items.result` / `result_preview` | `max_result_chars: 100000` | `result` 大文本可卸载 |
| `runs.first_human_message` / `last_ai_message` | DB `TEXT` | 保留（列表页需要，且已截断语义） |
| Uploads / 附件 | 单文件上限 50MB，**HEAD 已走对象存储**（`object_storage/uploads.py`） | 保持 |
| Agent workspace 变更 / thread outputs | **HEAD 已走对象存储**（`object_storage/outputs.py`） | 保持 |

---

## 12. Serialization Analysis

### 12.1 LangGraph 序列化格式实测

`langgraph-checkpoint` 4.1.1 的 `langgraph/checkpoint/serde/jsonplus.py`：

```python
def dumps_typed(self, obj: Any) -> tuple[str, bytes]:
    if obj is None:                  return "null", EMPTY_BYTES
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

- 默认序列化是 **msgpack（二进制）**，**不是 JSON**。
- 存在 **pickle 回退**（`pickle_fallback` 默认开启）→ blob 里可能出现**任意 Python 对象**。
- 类型标签（`"null"` / `"bytes"` / `"bytearray"` / `"json"` / `"msgpack"` / `"pickle"`）
  存在 `checkpoint_blobs.type` / `checkpoint_writes.type` 列里。

### 12.2 应用侧序列化现状（迁移的低摩擦基础）

| 位置 | 方式 |
| --- | --- |
| `persistence/engine.py:25-27` | `json.dumps(obj, ensure_ascii=False)`（`json_serializer`） |
| `runtime/events/store/db.py` `_content_to_db` | 非字符串内容 `json.dumps(default=str, ensure_ascii=False)` + metadata 标记 `content_is_json`；读回时按标记 `json.loads` |
| `persistence/agents/file.py` / `managed_subagents/file.py` | 文件型存储（JSON 文档） |
| `agents/memory/backends/deermem/` | 文件（`memory.json` manifest）+ SQLite FTS5 |

即：**应用层已经习惯"JSON 文本 + 显式类型标记"的序列化模式**，
这使得 §7.1 方案 A（JSON → TEXT）在应用侧是**低摩擦**的。

### 12.3 与"禁止 JSON/BLOB"的冲突面

| 冲突 | 位置 | 严重度 |
| --- | --- | --- |
| **JSON 列** | 25 个应用列 + `checkpoints.checkpoint`/`metadata` + `store.value` | 应用侧可解（TEXT）；LangGraph 侧不可解 |
| **BYTEA / BLOB 列** | `checkpoint_blobs.blob`、`checkpoint_writes.blob` | **不可解**（除非自研 saver） |
| **serialized Python object / msgpack / pickle** | JsonPlusSerializer | **不可解**（除非自研 saver 或改 serde） |
| **binary serialization** | 同上 | 同上 |

### 12.4 四个方案的可行性分析（本任务要求逐一分析）

#### 方案 A：TEXT Serialization

| 维度 | 分析 |
| --- | --- |
| **serialization format** | 应用侧：`json.dumps(ensure_ascii=False)`（沿用 `engine.py::_json_serializer`）。Checkpoint 侧：msgpack 是二进制，**不能直接放进 `TEXT`**，必须选：<br>(a) base64 包裹 msgpack（保真、体积 +33%）；<br>(b) 改为确定性 JSON 文本（需要实现一个新的 `SerializerProtocol`，且**必须处理 `bytes`/`tuple`/自定义类型**，否则会触发 pickle 回退）；<br>(c) 十六进制（体积 +100%，不推荐） |
| **size** | **关键问题**：`TEXT` 上限 **65,535 字节**。base64 后可用载荷约 **49 KB**。而 `messages` 通道是累积 reducer，`full` 模式下单个 blob 轻易超过 64KB。<br>改用 `MEDIUMTEXT`（16 MB）可解决体积问题，但**超出"优先使用 TEXT"的清单**（规范禁用清单未包含 `MEDIUMTEXT`，属灰色地带 → §20 Q6）。 |
| **performance** | 应用侧 JSON 序列化成本与 PG JSONB 的二进制解析相当；base64 编解码在 checkpoint 热路径上增加 CPU 与内存拷贝（每次 `get_tuple` / `put`）。TEXT 的比较/排序基于 collation，比 JSONB 弱，但应用侧不需要 DB 侧 JSON 查询 |
| **compatibility** | 与现有 `_content_to_db` / `_json_serializer` 模式完全兼容；与 `json_compat.py` 的方言 hack 冲突（应一并删除，改为应用层过滤） |
| **migration** | 机械：`sa.JSON` → `sa.Text`；读回时 `json.loads`。需逐列确认"是否有代码依赖 DB 侧 JSON 过滤"（只有 2 张表 / 5 个 key，见 §7.1） |
| **query requirements** | **必须拆列而非 TEXT** 的两处：`threads_meta.metadata_json`（pinned/archived 过滤，`thread_meta/sql.py:230,247,261`）、`runs.metadata_json`（3 个 key 的等值过滤，`run/sql.py:207,228-229`、`scheduled_task_runs/sql.py:847`） |
| **结论** | **适用 22/25 个应用列**。Checkpoint 侧**不适用**（体积超 TEXT 上限） |

#### 方案 B：Schema Normalization

| 维度 | 分析 |
| --- | --- |
| 适用 | 只适用于"结构稳定 + 需要过滤/排序"的少量列 |
| 反例（**明确不应规范化**） | `agents.config`（`agents/model.py` 的注释：新增 `AgentConfig` 字段必须零 Schema 变更往返）、`scheduled_tasks.schedule_spec`、`subagent_batches.execution_spec`、`mcp_tasks.result_artifact`、`projects.presentation` |
| 正例 | `threads_meta.metadata_json` → 拆 `pinned TINYINT(1) NOT NULL DEFAULT 0`、`archived TINYINT(1) NOT NULL DEFAULT 0` + 保留 `metadata_json` TEXT；`runs.metadata_json` → 拆 `regenerate_from_run_id VARCHAR(64) NULL`、`replay_kind VARCHAR(32) NULL`、`scheduled_task_run_id VARCHAR(64) NULL` |
| **结论** | 用于 **2 张表 / 5 个 scalar 列**（`threads_meta` 的 2 个 + `runs` 的 3 个），与方案 A 叠加使用，不替代 TEXT |

#### 方案 C：External Storage

| 维度 | 分析 |
| --- | --- |
| 适用 | `checkpoint_blobs.blob` / `checkpoint_writes.blob`（大且不需要 DB 侧查询）、`mcp_tasks.result_artifact`、`subagent_batch_items.result` |
| DB 侧保留 | `(thread_id, checkpoint_ns, channel, version, type, blob_ref, blob_size, blob_sha256)` |
| **成本（本版下调）** | **HEAD 已存在对象存储端口**（`object_storage/port.py` 的 `write_stream`/`open_read`/`stat`/`list_prefix`/`delete`/`copy`），且 `keys.py` 已定义稳定命名空间。因此新增 checkpoint blob 命名空间（如 `checkpoints/{thread_id}/{checkpoint_ns}/{channel}/{version}`）是**增量工作**，不是新建基础设施 |
| 优点 | 彻底绕开 `TEXT` 上限；blob 不进 DB，checkpoint 表保持轻量；与已有卸载模式一致 |
| 缺点 | **checkpoint 写入路径从 1 次 DB 写变成"对象存储写 + DB 写"**，必须处理部分失败（对象已写但 DB 未提交 → 孤儿对象，需 GC）；`get_tuple` 路径变成"DB 读 + N 次对象读"，延迟上升；且 checkpoint 的**唯一真相**从"单库事务"变成"跨系统最终一致" |
| **结论** | **对 checkpoint 大 blob 是唯一能同时满足"禁止 BLOB"与"不超 TEXT 上限"的方案**，但代价是重写 saver 读写路径 + 引入 GC + 接受跨系统一致性 |

#### 方案 D：Replace Component

| 维度 | 分析 |
| --- | --- |
| 触发条件 | 第三方 MySQL Saver 强制依赖 JSON/BLOB，违反规范 |
| 实测结论 | ✅ 触发：`langgraph-checkpoint-mysql` 3.0.0 使用 `JSON` + `LONGBLOB` + `BINARY(16)`（§8.4） |
| 三个子选项 | **(D1) fork / customize**：fork `tjni/langgraph-checkpoint-mysql`，把 `JSON`→`TEXT`、`LONGBLOB`→`TEXT`/对象存储、加 `ag_` 前缀与中文注释。**但**该包 SQL 大量依赖 `json_table`/`json_keys`/`json_extract`（用于从 `checkpoints.checkpoint` 提取 `channel_versions`），改成 TEXT 后这些 SQL **全部要重写** → 工作量接近自研。<br>**(D2) implement adapter**：自己实现 `BaseCheckpointSaver` 子类，直接针对目标 Schema 写 MySQL 方言 SQL。**推荐**。<br>**(D3) replace persistence component**：把 checkpoint 持久化从 LangGraph saver 抽象里拿出来，改为"应用侧 checkpoint 服务"。**不推荐**（破坏 LangGraph 的 durable execution 契约） |
| **结论** | **D2（自研 saver）**：以 `BaseCheckpointSaver` 为接口，针对 `ag_checkpoint*` 表写 MySQL 方言 SQL；serde 保持 `JsonPlusSerializer` 但**存 base64(TEXT) 或确定性 JSON**；大 blob 走方案 C |

### 12.5 序列化改造的推荐组合

```
ag_checkpoint.checkpoint        → TEXT       （确定性 JSON，结构由应用层保证）
ag_checkpoint.metadata          → TEXT
ag_checkpoint_blob.blob         → 小 blob 内联 MEDIUMTEXT(base64) + 大 blob 走方案 C（blob_ref）
ag_checkpoint_write.blob         → 同上
~~ag_store.value                  → TEXT~~   ⛔ 第五轮删除（Store 不迁移，§9）
应用侧 20 列（第五轮重算；原 22）  → TEXT（json.dumps ensure_ascii=False）
应用侧 3 列（threads_meta/runs 的 metadata）→ 方案 B 拆列
```

---

## 13. Transaction & Concurrency Analysis

### 13.1 隔离级别与错误模型

| 项 | PostgreSQL | MySQL 8.0.24 / InnoDB | 影响 |
| --- | --- | --- | --- |
| 默认隔离级别 | READ COMMITTED | **REPEATABLE READ** | 快照语义变化；gap lock 引入 |
| 显式设置 | 无（代码未设置 `isolation_level`） | 无 | 需在新实现里显式 `SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED`，否则所有并发假设需重新证明 |
| 唯一约束冲突 | SQLSTATE `23505` | 错误码 **1062** | 错误判定路径必须改写（`app/gateway/auth/repositories/sqlite.py:36-39` 依赖 asyncpg 异常形状） |
| 死锁 | 立即检测 + `40P01` | `innodb_deadlock_detect` 默认 ON，抛 **1213** | 行为相近；但 `innodb_lock_wait_timeout` 默认 **50 秒**，与 PG 的 `lock_timeout`（默认无限）不同 |
| 语句超时 | `command_timeout=30` 通过 asyncpg `connect_args` 传入（`database_config.py:194-198`） | MySQL 需要 `max_execution_time`（仅 SELECT）/ `innodb_lock_wait_timeout` | `_postgres_engine_kwargs`（`persistence/engine.py:30-50`）整个函数需要重写 |

### 13.2 Row Lock / `FOR UPDATE`（51 处，逐文件分布）

| 文件 | 处数 | 需要核验的索引 |
| --- | --- | --- |
| `persistence/subagent_batches/sql.py` | **14** | `idx_subagent_batch_items_claim(status, lease_expires_at, batch_id)`；`subagent_batches.status` 有 `ix_subagent_batches_status` |
| `persistence/scheduled_tasks/sql.py` | **11** | `ix_scheduled_tasks_next_run_at`（存在）、`ix_scheduled_tasks_status`（存在） |
| `persistence/mcp_tasks/sql.py` | **9** | `ix_mcp_tasks_due(status, next_poll_at)`、`ix_mcp_tasks_cancel_due`、`ix_mcp_tasks_notification_due`、`ix_mcp_tasks_next_poll_at`（均存在） |
| `persistence/scheduled_task_runs/sql.py` | **8** | `ix_scheduled_task_runs_status`；⚠️ **`(status, created_at)` 复合索引缺失**（FIFO claim 的 `ORDER BY attempt_count, created_at, id`） |
| `persistence/thread_meta/sql.py` | **6** | `ix_threads_meta_user_id` / `_assistant_id` / `_project_id` |
| `runtime/events/store/db.py` | 1 | `uq_events_thread_seq(thread_id, seq)` |
| `persistence/run/sql.py` | 1 | `ix_runs_lease(lease_expires_at)` |
| `persistence/projects/sql.py` | 1 | `ix_projects_user_id` / `_status` |

**风险**：MySQL 在无法使用索引时会锁定扫描到的**所有行**。
必须逐一核对每个 `with_for_update` 语句都有可用索引；上面标 ⚠️ 的 `scheduled_task_runs`
需要新增复合索引。

**RR 下的 gap lock**：`SELECT … FOR UPDATE` 在 RR 下对范围扫描加间隙锁，
会**改变并发插入行为**。`scheduled_task_runs/sql.py` 的 `claim_queued_run` 依赖
`WHERE id = ? AND status = 'queued' AND ~older_same_thread` 的条件 UPDATE 返回
`rowcount == 1` 判定抢占成功；RR 下该语句的锁行为与 RC 不同。

**PG 专有假设**：`mcp_tasks/sql.py:166-169` 的注释明确说
"`FOR SHARE` 会与老写入方的 `FOR NO KEY UPDATE` 冲突，而 `FOR KEY SHARE` 不会"。
这是 **PostgreSQL 四档行锁强度模型**独有的区分，**MySQL 没有这个模型**
（只有 S 锁 / X 锁 + 间隙锁）。该注释所依赖的正确性论证在 MySQL 上**不成立**，
必须重新设计归属校验。

### 13.3 SKIP LOCKED

- ✅ 8.0.1+ 可用；实测 SQLAlchemy 编译为 `… LIMIT %s FOR UPDATE SKIP LOCKED`。
- ⚠️ 约束：`SKIP LOCKED` 不能与 `NOWAIT` 同用；在 `UNION` / 聚合子查询中不可用
  （现有语句都是单表 `SELECT`，OK）。
- ⚠️ 无索引时 `SKIP LOCKED` 仍会扫全表（只是跳过被锁行）→ 性能退化而非错误。

### 13.4 UPSERT / Unique Conflict / RETURNING

| 用法 | PG | MySQL | 备注 |
| --- | --- | --- | --- |
| `ON CONFLICT DO NOTHING` | ✅ | `INSERT IGNORE`（吞所有错误）或 `ON DUPLICATE KEY UPDATE col = col` | **推荐后者**：只吸收唯一键冲突 |
| `ON CONFLICT DO UPDATE` | ✅ | `ON DUPLICATE KEY UPDATE` | 语义相近；`VALUES()` 已废弃（8.0.20+ 警告），应用行别名 `AS new`（8.0.19+） |
| ~~`ON CONFLICT DO UPDATE … WHERE <cond>`~~ | ~~✅~~ | ⛔ **第三轮后不再有调用方** | 原唯一使用点是 `dedupe_store.py`（§10.2 #5），已随渠道删除 |
| `RETURNING` | ✅ | ❌ **不支持**，且 **SQLAlchemy 不会报错**（§10.2 #17） | **4 处**必须重构（第三轮后；原 5 处） |
| `UPDATE … RETURNING` 的替代 | — | **`rowcount` 判定**（纯条件 UPDATE 可靠）或 **`SELECT … FOR UPDATE` + `UPDATE` 两步法** | 逐个判定，见下表 |

**4 处 `RETURNING` 的逐条替代设计**（第三轮后；原 5 处）

| 位置 | 语义 | MySQL 替代 | 难度 |
| --- | --- | --- | --- |
| `persistence/run/sql.py:563` `renew_lease` | 续约成功与否 + 读回 `cancel_action` | `UPDATE … WHERE …` → `result.rowcount == 1` 判定续约；随后**同事务内** `SELECT cancel_action WHERE run_id=?`（该行已被 X 锁，读值一致） | Easy |
| `persistence/run/sql.py:594` `request_cancel` | 读回"首次胜出"的 `cancel_action` | 同事务内 `SELECT … FOR UPDATE` 取当前值 → Python 计算 `case` 结果 → `UPDATE` 写入 | Easy |
| `persistence/run/sql.py:627` `finalize_if_not_cancelled` | 是否完成了 finalize | `result.rowcount == 1`（纯条件 UPDATE，无 upsert）→ **已是最简形式**；代码里 `:540` 已有 `rowcount != 0` 的同类用法可参照 | Easy |
| `persistence/scheduled_task_runs/sql.py:211` `occurrence_seq` | 序号分配 | **两步法**：`SELECT last_occurrence_seq … FOR UPDATE` → Python `+1` → `UPDATE`。⚠️ **不要用 `LAST_INSERT_ID(expr)`**：`run_events.id` 是 AUTO_INCREMENT，同一连接/事务内的 INSERT 会覆盖 `LAST_INSERT_ID()` 的值，导致序号串号 | Moderate |
| ~~`app/channels/dedupe_store.py:153`~~ | ⛔ **随渠道删除消失** | ~~`SELECT … FOR UPDATE`（判定 TTL 是否过期）→ 分支 `INSERT` 或 `UPDATE first_seen`~~ | ~~**Hard**~~ → **已消除** |

### 13.5 并发 Checkpoint 写入（场景 A）

- **现有保护**：`uq_runs_thread_active` 保证"每线程至多一个活跃 run"，
  因此 checkpoint 并发写的**正常路径是串行的**。
- **MySQL 变化**：
  - partial unique → 生成列唯一索引后**唯一性语义等价**，但错误码 `23505` → `1062`。
  - 真正并发写同一 `thread_id` 的 checkpoint（如 takeover 后的双写）：
    PG 用 `ON CONFLICT DO UPDATE` 后写覆盖；MySQL 的 `ON DUPLICATE KEY UPDATE` 同样覆盖，
    但 **RR 下更易死锁**；死锁时 MySQL 抛 `1213`、PG 抛 `40P01`，上层重试策略需识别新错误码。

### 13.6 时间语义（一个容易漏掉的破坏点）

| 问题 | 证据 | 后果 |
| --- | --- | --- |
| MySQL `DATETIME` 默认精度为 0 | 实测：`DateTime(timezone=True)` 在 MySQL 方言下编译为 **`DATETIME`**（PG 是 `TIMESTAMP WITH TIME ZONE`） | **亚秒被截断**。`scheduled_task_runs` 的 FIFO 排序是 `order_by(created_at.asc(), id.asc())`，`expire_queued_runs` 用 Python 计算带微秒的 `created_before` 比较 → 截断后**排序与过期判定都会错** |
| `DateTime(timezone=True)` 在 MySQL 上**静默丢弃时区** | MySQL 无时区感知的 `DATETIME` | 只要全部写入都是 `datetime.now(UTC)`（offset 0），wall clock 恰好等于 UTC，**"碰巧正确"**。一旦有任何非 UTC 写入即静默错误 |
| 读回是 naive datetime | SQLite 已有此问题（`events/store/db.py` 有注释） | 代码里已有 `coerce_iso` 与 `_lease_is_alive` 的 `replace(tzinfo=UTC)` 兜底，但**不是所有路径都覆盖**，需要系统审计 |
| `TIMESTAMP` 不可用 | 2038 上限 + 隐式时区换算 | 必须用 `DATETIME(6)`，不能用 `TIMESTAMP` |
| 全库影响面 | **48 个** `DateTime(timezone=True)` 列（第三轮后；原 61） | 需要逐列改为 `mysql.DATETIME(fsp=6)` |

**建议**：所有时间列显式改为 `mysql.DATETIME(fsp=6)`，并在应用侧统一
"UTC aware → naive UTC" 的写入边界转换。

### 13.7 `run_events.seq` 的分配语义：**不能只凭"编译通过"判定等价**（V3）

> **本节是对本版第一轮结论的自我修正。** 第一轮把该项判为 **Easy**，理由是
> "`SELECT max(…) FOR UPDATE` 在 MySQL 编译正常 → 现有 `else` 分支已经是正确实现 →
> advisory-lock 分支可删除"。**该推理不成立**，本版改为 **Moderate** 并新增实测项 V3。

#### 13.7.1 现状代码的三个事实

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

| # | 事实 | 代码位置 |
| --- | --- | --- |
| 1 | **只有 `postgresql` 有专属分支**；`else` 覆盖其余所有方言，包括 MySQL | `db.py:146-153` |
| 2 | `else` 分支的正确性来源是 **SQLite**：SQLite 方言**忽略** `FOR UPDATE`（渲染为空），其串行化来自"整库单写者锁" —— **与 `FOR UPDATE` 无关** | 同上 + `db.py:137-141` 的 docstring（"Other dialects keep the existing row-locking statement"） |
| 3 | 单进程内的竞态由**进程内 `asyncio.Lock`** 兜住（per `thread_id`）；DB 层语句**只对跨进程写者有意义** | `db.py:32-45`（`_write_locks`） |

**因此真正的问题只有一个**：`else` 分支在 **MySQL + 跨进程**下是否仍能串行化。
`run_events` 已有 `uq_events_thread_seq(thread_id, seq)`（`persistence/models/run_event.py:33`），
**唯一约束会兜住重复**，但代码里**没有任何重试**（`runtime/events/` 全目录无 `IntegrityError` 处理），
所以"兜住"的实际表现是**整个 batch 事务失败**，而不是"重试一次就好"。

#### 13.7.2 为什么"编译通过"不能作为证据

这与本版 §10.2 #17 已经指出的 `RETURNING` 陷阱是**同一类错误**：

| | 编译期 | 运行期 | 本版第一轮的处理 |
| --- | --- | --- | --- |
| `UPDATE … RETURNING` | ✅ SQLAlchemy 不报错 | ❌ MySQL 1064 | 正确识别为**静默陷阱**，要求 CI 静态检查 |
| `SELECT max(…) … FOR UPDATE` | ✅ SQLAlchemy 不报错 | ⚠️ **语法可执行，但串行化语义未验证** | ❌ **误判为"已经是正确实现"** |

后者比前者更隐蔽：它**不报错、能跑通、单测能过、小数据量下永远看不出问题**，
只在"多 Worker + 同一 thread 并发写事件"时表现为序号冲突或事务失败。

#### 13.7.3 需要实测的三个具体问题（V3）

> **V3 — `run_events.seq` 在 MySQL 8.0.24 + READ COMMITTED 下的并发分配语义实测。**

| # | 待测问题 | 为什么它是问题 |
| --- | --- | --- |
| **V3-a** | `SELECT max(seq) FROM run_events WHERE thread_id = ? FOR UPDATE` 在 MySQL 8.0.24 上**实际锁住了什么**？ | 需确认优化器是否走 `uq_events_thread_seq(thread_id, seq)` 的**反向索引扫描**并锁定"最大 seq 那一行"；若退化为全表扫描 + 临时聚合，锁足迹完全不同 |
| **V3-b** | **该 thread 尚无任何 event 行**（`max(seq)` 为 `NULL`）时，语句**是否锁定任何行**？ | 🔴 **最可疑的一格**：无行可锁 → 两个并发事务都读到 `NULL` → 都从 `seq = 1` 开始插入。RC 下**不加 gap lock**，无法阻止"锁住的行旁边插入新行"，因此**空 thread 场景很可能完全没有串行化** |
| **V3-c** | 两个并发事务同时执行"读 max → 插入"时，是否会出现**双双读到同一 max 并各自插入**？ | 若出现，则 `uq_events_thread_seq` 会以 **1062** 拒绝其中一个 → 因为**没有重试**，该 batch 直接失败（事件丢失），而不是自动纠正 |

**实测方法**：两个独立连接 / 独立事务，在 **RC** 隔离级别下并发执行
`put_batch`，用 `performance_schema.data_locks` 或 `SHOW ENGINE INNODB STATUS`
观察实际锁对象；分别覆盖 **有行** / **空 thread** / **同进程 vs 跨进程** 三种组合。
（不要只用"能跑通"作为判据 —— 必须制造竞态并观察 1062 是否出现。）

#### 13.7.4 若不能保证串行化：三个候选方案

| 方案 | 做法 | 评价 |
| --- | --- | --- |
| **(a) per-thread counter / sentinel row + `SELECT … FOR UPDATE`** | 为每个 `thread_id` 维护一行"序号锚点"，`SELECT … FOR UPDATE` 该锚点行 → `+1` → `UPDATE` | ✅ **推荐**。锚点行**总是存在**，因此不存在 V3-b 的"无行可锁"空洞。且与 §20.7 已决策的调度器预算锁（**锁表 sentinel row + `SELECT … FOR UPDATE`**）是**同一个模式**，不引入新机制。代价：需新增一张表或复用 `threads_meta` 加列，并处理锚点的创建竞态（`INSERT … ON DUPLICATE KEY UPDATE`） |
| **(b) 唯一约束 + bounded retry** | 保留现语句，捕获 1062 后重读 max 并重试（带次数上限 + 退避） | ✅ 可用，但**必须新写重试代码**（当前没有）；且冲突率随并发写者数量上升，高并发下会变成"重试风暴"。适合作为 (a) 的**兜底**而非主方案 |
| **(c) `INSERT … ON DUPLICATE KEY UPDATE seq = seq + 1` + 读回** | 把序号分配合并进一次原子 upsert | ⚠️ 需先有锚点行，本质是 (a) 的变体；且要注意 `run_events.id` 是 `AUTO_INCREMENT`，**不要用 `LAST_INSERT_ID(expr)`**（同 §13.4 的污染风险） |

**明确不采用**：`GET_LOCK()`（连接级，池化后不释放 —— §20.7 已否决）。
**也不采用**：把 `seq` 交给 Redis 分配（序号是**排序真相源**，volatile Redis 丢失会跳号/重号 —— §18.2 (C)）。

#### 13.7.5 该项的归类

- **难度：Moderate**（不是 Easy）。工作量本身不大（一个锚点表 + 一段分配逻辑 + 并发测试），
  但**必须实测 + 必须重新设计**，不能"删掉 advisory-lock 分支就收工"。
- **是否阻塞迁移**：**是**，但属于"阶段 4 的并发正确性验收 Gate"，不是"建表就失败"那类硬阻塞。
  它的存在**不改变** `Feasible with significant changes` 的结论档位 ——
  只是让"并发语义替换"这一代价的**下限抬高了一点**。
- **风险等级**：与 §17.2 风险 2（并发语义变化）同类，即"**能跑但可能静默错误**"。
  唯一约束使它**不会静默写坏数据**（会以 1062 显式失败），但会**丢事件**。

---

## 14. Schema Compatibility Matrix

目标表名统一使用 `ag_` 前缀。

| Current Table | Purpose | PostgreSQL-specific Features | Target MySQL Table | Schema Change | Code Change | Risk |
| --- | --- | --- | --- | --- | --- | --- |
| `runs` | Run 元数据、token 统计、多 Worker 租约 | `JSON`×3、partial unique `uq_runs_thread_active`、`metadata_json ->> 'x'` 过滤、3 处 `RETURNING` | `ag_runs` | 3×JSON→TEXT（`metadata_json` 另拆 3 个 scalar 列）、partial unique → 生成列唯一索引、`DATETIME(6)`、补 `DEFAULT`、补中文注释 | `persistence/run/model.py`、`persistence/run/sql.py:207,228-229,563,594,627`、`runtime/runs/*`、`runtime/runs/store/base.py` 的 `IntegrityError` 判定 | **High** |
| `run_events` | 运行事件流（含 seq 分配） | `JSON`（`event_metadata`）、`autoincrement` PK（PG 渲染 `SERIAL`）、`uq_events_thread_seq`、advisory lock 分配 seq | `ag_run_events` | `event_metadata`→TEXT、`id` 保持 `INT AUTO_INCREMENT`（PG 侧渲染为 `SERIAL`；**建议评估是否升为 `BIGINT`**，事件流是增长最快的表）、`content` TEXT（不可作索引，现状即如此）、`TEXT` 默认值用表达式形式 | `runtime/events/store/db.py:146-153`：**删除 advisory-lock 分支**，直接走已有 `else`（`FOR UPDATE`） | Moderate |
| `threads_meta` | 线程元数据（含 pinned/archived） | `JSON`（`metadata_json`）+ **JSON key 过滤**（`json_compat.py`） | `ag_threads_meta` | `metadata_json` 拆 `pinned`/`archived` + 保留 TEXT；删掉 `JsonMatch` 依赖 | `persistence/thread_meta/sql.py:230,247,261`、`persistence/json_compat.py`（整体废弃） | Moderate |
| `users` | 账号 + OAuth 关联 | partial unique `idx_users_oauth_identity` | `ag_users` | partial unique → 生成列唯一索引（或函数索引）；`id` 保持 `VARCHAR(36)` | `app/gateway/auth/repositories/sqlite.py:36-39` 的约束名/错误码判定 | Moderate |
| `user_preferences` | 逐 key 偏好（独立 patch） | `JSON`（`value`）、**FK**（→`users.id` CASCADE）、`pg_insert.on_conflict_do_update` | `ag_user_preferences` | `value`→TEXT、**删 FK**（级联删除移到应用层）、upsert 改 MySQL 方言 | `persistence/user/model.py:37`、`persistence/user/preferences.py:4,21-25`、级联删除需新实现 | Moderate |
| `agents` | 自定义 Agent 定义（整文档） | `JSON`（`config`）、**未命名** `UniqueConstraint(name)` | `ag_agents` | `config`→TEXT（**不规范化**）、补唯一约束名、`soul` TEXT 默认值用表达式形式 | 读写路径 JSON 编解码 | Easy |
| `projects` | 项目分组 | `JSON`（`presentation`） | `ag_projects` | `presentation`→TEXT、`instructions` 默认值表达式形式 | 编解码 | Easy |
| `feedback` | 用户反馈 | 无（纯 scalar） | `ag_feedback` | 时间列精度、补中文注释 | 无 | Easy |
| `personal_access_tokens` | PAT（scopes 为 JSON 数组） | `JSON`（`scopes`）、unique `token_digest` | `ag_personal_access_tokens` | `scopes`→TEXT（或逗号分隔 `VARCHAR`，§20 Q4） | 编解码 | Easy |
| `scheduled_tasks` | 定时任务定义 | `JSON`（`schedule_spec`）、`SKIP LOCKED`、`FOR UPDATE`、advisory lock（兄弟表） | `ag_scheduled_tasks` | `schedule_spec`→TEXT、`last_occurrence_seq` 保持 `BIGINT NOT NULL DEFAULT 0`、时间列精度 | `persistence/scheduled_tasks/sql.py` 的 `with_for_update` × 11 需索引核验；`occurrence_seq` 的 `RETURNING` 替代 | **High** |
| `scheduled_task_runs` | 调度 occurrence（队列） | `UPDATE … RETURNING`、`pg_advisory_xact_lock`、partial unique `uq_scheduled_task_run_active`、`FOR UPDATE` × 8 | `ag_scheduled_task_runs` | `occurrence_seq`/`launch_accounted` backfill 后 `NOT NULL`、partial unique → 生成列唯一索引、`RETURNING` → 两步法、**新增 `(status, created_at)` 索引** | `persistence/scheduled_task_runs/sql.py:203-212,317,754-763` | **High** |
| `mcp_tasks` | 远程 MCP 任务轮询 | `JSON`×5、`SKIP LOCKED`、`FOR SHARE`（PG 行锁强度假设）、lease + `dispatch_version` 栅栏 | `ag_mcp_tasks` | 5×JSON→TEXT、`FOR SHARE` 归属校验需重设计、时间列精度、`dispatch_version` backfill | `persistence/mcp_tasks/sql.py:161-169,230,268,303,339,376,413,480,589` | **High** |
| `managed_subagents` | 托管子 Agent 定义 | `JSON`（`definition`） | `ag_managed_subagents` | `definition`→TEXT、补唯一约束名 | 编解码 | Easy |
| `subagent_batches` | 批量子 Agent 提交 | `JSON`（`execution_spec`） | `ag_subagent_batches` | `execution_spec`→TEXT | `persistence/subagent_batches/sql.py` 的 `with_for_update` × 14 需索引核验 | Moderate |
| `subagent_batch_items` | 批量条目（含 lease） | `JSON`×3、**FK**、`SKIP LOCKED` | `ag_subagent_batch_items` | 3×JSON→TEXT、删 FK（级联删除移到应用层）、补 `result_truncated` 默认值 | 同上 + 级联删除 | Moderate |
| ~~`channel_connections`~~ | ~~IM 渠道连接~~ | ⛔ **第三轮删除**（原：`JSON`×3、partial unique、advisory lock） | — | — | — | **已移出范围** |
| ~~`channel_credentials`~~ | ~~渠道凭据（加密载荷）~~ | ⛔ **第三轮删除**（原：FK CASCADE） | — | — | — | **已移出范围** |
| ~~`channel_oauth_states`~~ | ~~OAuth 状态机~~ | ⛔ **第三轮删除**（原：`JSON`×2） | — | — | — | **已移出范围** |
| ~~`channel_conversations`~~ | ~~外部会话 → 线程映射~~ | ⛔ **第三轮删除**（原：FK CASCADE） | — | — | — | **已移出范围** |
| ~~`webhook_deliveries`~~ | ~~跨 Pod 入站去重~~ | ⛔ **第三轮删除** —— 原为全表**唯一 High（最高）**难度项（条件 upsert + 8448 字节主键 + 去重时机调整） | — | — | — | **已移出范围** |
| `alembic_version` | Alembic 版本行 | 无 | `ag_alembic_version` | 配置 `version_table` | `persistence/bootstrap.py:361,416,290-327`、`migrations/env.py` | Moderate |
| `checkpoints` | LangGraph checkpoint 头 | **`JSONB`×2**、`TEXT` 主键、`CREATE INDEX CONCURRENTLY`、`jsonb_each_text`、`%s` 占位符 | `ag_checkpoint` | 全表重建：`checkpoint`/`metadata`→TEXT、`thread_id`/`checkpoint_ns`/`checkpoint_id` `TEXT`→`VARCHAR(N)`、索引语法改写、补中文注释 | **自研 saver** | **High** |
| `checkpoint_blobs` | 通道值 blob | **`BYTEA`**、`TEXT` 主键、`CONCURRENTLY` | `ag_checkpoint_blob` | `blob`→ 方案 C（对象存储 + 引用列）或 `MEDIUMTEXT(base64)`；主键列收紧为 `VARCHAR(N)` | **自研 saver + 对象存储客户端** | **High** |
| `checkpoint_writes` | 待处理写入 | **`BYTEA NOT NULL`**、`TEXT` 主键、`CONCURRENTLY` | `ag_checkpoint_write` | 同上 + `task_path` | 同上 | **High** |
| `checkpoint_migrations` | LangGraph 自身版本表 | 无 | `ag_checkpoint_migration` | 改名即可（但由第三方 `setup()` 管理 → 需自研） | 自研 saver | Moderate |
| ~~`store`~~ | ~~LangGraph Store 文档~~ | ⛔ **第五轮删除**（原：`value JSON`、`text_pattern_ops` 索引、`unnest(%s::text[])`） | — | **不迁移**（§9） | — | **已移出范围** |

---

## 15. Index Analysis

### 15.1 命名规范改造

规范：所有 index / constraint 名 ≤ 100 字符；unique → `uk_<field_name>`；
普通 → `idx_<field_name>`；composite 使用简短可读名。

**完整重命名映射（**第三轮后 47 个索引**；原 58 个）**

> ⛔ **第三轮删除的 11 个渠道索引**（不再需要重命名，因为表本身要删）：
> `idx_channel_connections_event_lookup`、`ix_channel_connections_provider`、
> `ix_channel_connections_owner_user_id`、`uq_channel_connection_active_identity`、
> `ix_channel_conversations_thread_id`、`ix_channel_conversations_provider`、
> `ix_channel_conversations_owner_user_id`、`ix_channel_conversations_connection_id`、
> `ix_channel_oauth_states_provider`、`ix_channel_oauth_states_owner_user_id`、
> `ix_webhook_deliveries_first_seen`。

| 现名 | 目标名 | 列 | 类型 |
| --- | --- | --- | --- |
| `ix_agents_user_id` | `idx_agents_user_id` | user_id | index |
| `ix_feedback_thread_id` | `idx_feedback_thread_id` | thread_id | index |
| `ix_feedback_user_id` | `idx_feedback_user_id` | user_id | index |
| `ix_feedback_run_id` | `idx_feedback_run_id` | run_id | index |
| `ix_mcp_tasks_notification_due` | `idx_mcp_tasks_notification_due` | notification_status_next_notification_at | index |
| `ix_mcp_tasks_thread_id` | `idx_mcp_tasks_thread_id` | thread_id | index |
| `ix_mcp_tasks_cancel_due` | `idx_mcp_tasks_cancel_due` | cancel_requested_at_next_cancel_at | index |
| `ix_mcp_tasks_due` | `idx_mcp_tasks_due` | status_next_poll_at | index |
| `ix_mcp_tasks_notification_status` | `idx_mcp_tasks_notification_status` | notification_status | index |
| `ix_mcp_tasks_status` | `idx_mcp_tasks_status` | status | index |
| `ix_mcp_tasks_user_id` | `idx_mcp_tasks_user_id` | user_id | index |
| `ix_mcp_tasks_thread_created` | `idx_mcp_tasks_thread_created` | thread_id_created_at | index |
| `ix_mcp_tasks_next_poll_at` | `idx_mcp_tasks_next_poll_at` | next_poll_at | index |
| `ix_personal_access_tokens_token_digest` | `uk_personal_access_tokens_token_digest` | token_digest | unique |
| `ix_personal_access_tokens_user_id` | `idx_personal_access_tokens_user_id` | user_id | index |
| `ix_projects_user_id` | `idx_projects_user_id` | user_id | index |
| `ix_projects_status` | `idx_projects_status` | status | index |
| `ix_events_thread_cat_seq` | `idx_events_thread_cat_seq` | thread_id_category_seq | index |
| `ix_run_events_user_id` | `idx_run_events_user_id` | user_id | index |
| `ix_events_run` | `idx_events_run` | thread_id_run_id_seq | index |
| `uq_runs_thread_active` | `uk_runs_thread_active` | thread_id | unique（partial → 生成列） |
| `ix_runs_lease` | `idx_runs_lease` | lease_expires_at | index |
| `uq_runs_idempotency_key` | `uk_runs_idempotency_key` | idempotency_key | unique |
| `ix_runs_thread_status` | `idx_runs_thread_status` | thread_id_status | index |
| `ix_runs_user_id` | `idx_runs_user_id` | user_id | index |
| `ix_runs_thread_id` | `idx_runs_thread_id` | thread_id | index |
| `uq_scheduled_task_run_occurrence_seq` | `uk_scheduled_task_run_occurrence_seq` | task_id_occurrence_seq | unique |
| `ix_scheduled_task_runs_task_id` | `idx_scheduled_task_runs_task_id` | task_id | index |
| `ix_scheduled_task_runs_status` | `idx_scheduled_task_runs_status` | status | index |
| `uq_scheduled_task_run_active` | `uk_scheduled_task_run_active` | task_id | unique（partial → 生成列） |
| `ix_scheduled_task_runs_thread_id` | `idx_scheduled_task_runs_thread_id` | thread_id | index |
| `ix_scheduled_tasks_user_id` | `idx_scheduled_tasks_user_id` | user_id | index |
| `ix_scheduled_tasks_thread_id` | `idx_scheduled_tasks_thread_id` | thread_id | index |
| `ix_scheduled_tasks_next_run_at` | `idx_scheduled_tasks_next_run_at` | next_run_at | index |
| `ix_scheduled_tasks_status` | `idx_scheduled_tasks_status` | status | index |
| `ix_subagent_batch_items_batch_id` | `idx_subagent_batch_items_batch_id` | batch_id | index |
| `ix_subagent_batch_items_status` | `idx_subagent_batch_items_status` | status | index |
| `ix_subagent_batch_items_claim` | `idx_subagent_batch_items_claim` | status_lease_expires_at_batch_id | index |
| `ix_subagent_batches_thread_id` | `idx_subagent_batches_thread_id` | thread_id | index |
| `ix_subagent_batches_thread_created` | `idx_subagent_batches_thread_created` | thread_id_created_at | index |
| `ix_subagent_batches_status` | `idx_subagent_batches_status` | status | index |
| `ix_subagent_batches_user_id` | `idx_subagent_batches_user_id` | user_id | index |
| `ix_threads_meta_assistant_id` | `idx_threads_meta_assistant_id` | assistant_id | index |
| `ix_threads_meta_user_id` | `idx_threads_meta_user_id` | user_id | index |
| `ix_threads_meta_project_id` | `idx_threads_meta_project_id` | project_id | index |
| `ix_users_email` | `uk_users_email` | email | unique |
| `idx_users_oauth_identity` | `uk_users_oauth_identity` | oauth_provider_oauth_id | unique（partial → 生成列） |
| ~~`ix_webhook_deliveries_first_seen`~~ | ⛔ 表已删除 | first_seen | — |

**长度核验**：第三轮后最长的目标名是 `uk_scheduled_task_run_occurrence_seq`（38 字符）
与 `uk_personal_access_tokens_token_digest`（39 字符），全部 ≤ 100，安全。
（原最长项 `uk_channel_connection_owner_provider_identity` 46 字符已随渠道删除。）

**未命名唯一约束**：`managed_subagents.name` 必须补名（建议 `uk_managed_subagents_name`）。

### 15.2 必须保留的查询覆盖（不得因去重而破坏）

| 查询模式 | 位置 | 依赖索引 |
| --- | --- | --- |
| 每线程一个活跃 run（唯一性 + reject 策略） | `runtime/runs/manager.py`、`runtime/runs/store/base.py` | `uk_runs_thread_active`（生成列） |
| 租约过期扫描 | `persistence/run/sql.py`、`scheduled_task_runs/sql.py:569-572` | `idx_runs_lease` |
| 事件流分页（`thread_id` + `category` + `seq`） | `runtime/events/store/db.py` | `idx_events_thread_cat_seq`（leftmost prefix 覆盖 `thread_id`） |
| 事件序号唯一性 | 同上 | `uk_events_thread_seq` |
| 线程列表按用户 + 助手过滤 | `persistence/thread_meta/sql.py` | `idx_threads_meta_user_id`、`idx_threads_meta_assistant_id` |
| due-task claim（`next_run_at` + lease 过期） | `persistence/scheduled_tasks/sql.py:320-332` | `idx_scheduled_tasks_next_run_at` ✅ 已存在 |
| occurrence 队列 claim（`status='queued'` + `created_at`） | `persistence/scheduled_task_runs/sql.py:270-285` | ⚠️ **`(status, created_at)` 索引缺失，需新增** |
| MCP poll / cancel / notification claim | `mcp_tasks/sql.py:220-230,370-376,470-480` | `idx_mcp_tasks_due`、`idx_mcp_tasks_cancel_due`、`idx_mcp_tasks_notification_due` ✅ 均已存在 |
| subagent batch item claim | `persistence/subagent_batches/sql.py` | `idx_subagent_batch_items_claim` ✅ 已存在 |
| OAuth identity 唯一性 | `app/gateway/auth/repositories/sqlite.py` | `uk_users_oauth_identity`（生成列） |
| ~~webhook 去重~~ | ⛔ 随渠道删除 | — |
| run → scheduled occurrence 反查 | `scheduled_task_runs/sql.py:847` | **拆列后**建 `idx_runs_scheduled_task_run_id` |
| ~~credential / conversation 按 connection 反查~~ | ⛔ 随渠道删除 | — |

> 查询覆盖清单由 **13 行缩为 11 行**（删除 2 行渠道相关）。

### 15.3 Partial Index 的生成列替代方案（关键）

**三个** partial unique index（第三轮后；原四个）全部可以**精确等价**地映射为生成列唯一索引
（因为 MySQL 唯一索引允许任意多个 `NULL`）：

**① `uq_runs_thread_active`（`run/model.py:56-70`）**

```sql
-- 设计示意（不在本阶段执行）
active_thread_id VARCHAR(64)
  GENERATED ALWAYS AS (IF(status IN ('pending','running'), thread_id, NULL)) STORED,
UNIQUE KEY uk_runs_thread_active (active_thread_id)
```

**② `uq_scheduled_task_run_active`（`scheduled_task_runs/model.py`）**

```sql
active_task_id VARCHAR(64)
  GENERATED ALWAYS AS (IF(status IN ('queued','launching','running'), task_id, NULL)) STORED,
UNIQUE KEY uk_scheduled_task_run_active (active_task_id)
```

**③ ~~`uq_channel_connection_active_identity`（`channel_connections/model.py`）~~** ⛔ **第三轮后作废**

> 表已删除，本节原设计（`active_identity_key` 生成列 + 3 列拼接）**不再实施**。
> 原文保留在下方仅作历史记录：
>
> ```sql
> active_identity_key VARCHAR(320)
>   GENERATED ALWAYS AS (
>     IF(status != 'revoked',
>        CONCAT(provider, ':', external_account_id, ':', workspace_id), NULL)) STORED,
> UNIQUE KEY uk_channel_connection_active_identity (active_identity_key)
> ```
>
> ⚠️ 其中的教训**仍然通用**（未来若再建多列拼接的生成列唯一索引）：
> `CONCAT` 的分隔符必须使用**不会出现在取值中的字符**（`:` 可能出现在
> `external_account_id` 中）——建议改用 `CHAR(1)` 的 `\x1f`（US，unit separator）
> 或 `CONCAT_WS('\x1f', …)`，并加中文 COMMENT 说明。

**④ `idx_users_oauth_identity`（`user/model.py:88-95`，索引名常量在 `:30`，谓词在 `:93-94`）**

```sql
oauth_identity_key VARCHAR(160)
  GENERATED ALWAYS AS (
    IF(oauth_provider IS NOT NULL AND oauth_id IS NOT NULL,
       CONCAT_WS('\x1f', oauth_provider, oauth_id), NULL)) STORED,
UNIQUE KEY uk_users_oauth_identity (oauth_identity_key)
```

或使用**函数索引**（8.0.13+）：

```sql
UNIQUE KEY uk_users_oauth_identity (
  (IF(oauth_provider IS NOT NULL AND oauth_id IS NOT NULL,
      CONCAT_WS('\x1f', oauth_provider, oauth_id), NULL)))
```

**二选一权衡**：生成列更可读（可加中文 COMMENT、可被 `SELECT` 引用）；
函数索引无需新增列。**推荐生成列**，因为本规范要求中文 COMMENT，
而函数索引的表达式无法注释。

⚠️ 与 `0018_oauth_identity_pg_partial.py` 的关系：该 revision 读 PG 系统目录，
在 MySQL 上不适用；MySQL baseline 直接建生成列唯一索引即可。

### 15.4 需要新增的索引（现在缺失或靠 FK 隐式提供）

| 目标索引 | 表 | 理由 |
| --- | --- | --- |
| `idx_scheduled_task_runs_status_created` | `ag_scheduled_task_runs` | occurrence 队列 claim 的 `WHERE status='queued' ORDER BY attempt_count, created_at, id`（现有只建了活跃去重索引） |
| `idx_runs_scheduled_task_run_id` | `ag_runs` | 替代 `metadata_json ->> 'scheduled_task_run_id'` 过滤（`scheduled_task_runs/sql.py:847`） |
| `idx_runs_regenerate_from_run_id` | `ag_runs` | 替代 `metadata_json ->> 'regenerate_from_run_id'` 过滤（`run/sql.py:207`） |
| `idx_threads_meta_pinned_archived` | `ag_threads_meta` | 替代 `JsonMatch` 的置顶/归档排序（`thread_meta/sql.py:230,247,261`） |
| ~~`idx_channel_credentials_connection_id`~~ | ⛔ 表已删除 | ~~FK 移除后显式建立~~（原注：`connection_id` 是 PK，实际无需新增，此行仅为记录已核验） |
| `idx_subagent_batch_items_batch_id` | `ag_subagent_batch_items` | 已有 `ix_subagent_batch_items_batch_id`，仅重命名 |
| ~~`uk_webhook_deliveries_key`~~ | ⛔ 表已删除 | ~~替代超限的 4 列复合主键~~ |

> **第三轮效果**：新增索引清单由 **7 项缩为 5 项**（删除 2 项，其中 1 项本就是"核验后无需新增"）。
> 真正需要新增的索引只有 **4 个**，且**全部与渠道无关**。

### 15.5 需要删除的冗余索引（核验结论：**不建议删除任何索引**）

| 候选 | 核验结论 |
| --- | --- |
| `ix_runs_thread_id` | ❌ **不能删**：`uk_runs_thread_active`（生成列）对终态行为 `NULL`，无法覆盖 `thread_id` 等值查询 |
| `ix_threads_meta_user_id` | 若新增 `idx_threads_meta_user_status_activity` 复合索引，leftmost prefix 可覆盖 → 但**需先确认无 `user_id` 单独查询**，本阶段不删 |
| `ix_feedback_thread_id` | 若查询总是带 `thread_id` + `user_id` → 但 `uq_feedback_thread_run_user` 的 leftmost prefix 是 `thread_id`，已覆盖 → **可删候选**，但收益小 |
| `ix_run_events_user_id` | 与 `idx_events_thread_cat_seq` 前导列不同 → **保留** |
| `ix_mcp_tasks_notification_status` | 是 `ix_mcp_tasks_notification_due` 的 leftmost prefix → **可删候选** |
| `ix_mcp_tasks_status` | 是 `ix_mcp_tasks_due` 的 leftmost prefix → **可删候选** |
| `ix_scheduled_task_runs_status` | 若新增 `idx_scheduled_task_runs_status_created` → **可删候选** |
| `ix_subagent_batch_items_status` | 是 `ix_subagent_batch_items_claim` 的 leftmost prefix → **可删候选** |

**结论**：本次迁移**不主动删除任何现有索引**（除重命名外）。
理由是 MySQL 的锁行为对索引缺失更敏感（§13.2，"无索引 = 锁全表"），
删除索引的风险高于收益。若确需去重，应在 MySQL 上跑 `EXPLAIN` + 锁范围实测后再决定。

### 15.6 ~~`webhook_deliveries` 主键重设计（唯一越界表）~~ ⛔ **已作废（第三轮）**

> ⛔ **本节结论已被第三轮范围决策取代，不再实施。**
>
> `webhook_deliveries` 表随 Channel / GitHub Webhook **整体删除**（§1.5），
> 因此这里设计的 `delivery_key CHAR(64)` 代理主键、连接解析时机前移/后移、
> 哈希规范化与 fail-closed 语义保持等**全部工作项都不需要做**。
> 相应地，§20.8 中关于该表主键的决策也一并作废。
>
> **本节保留仅为历史记录** —— 它记录了"如果保留渠道，MySQL 会遇到的
> 最难的一个 Schema 问题"。保留它的价值在于：**未来重新设计 Custom Channel 时，
> 若仍要在 MySQL 上做跨实例入站去重，本节的分析与陷阱清单可直接复用**
> （尤其是"去重发生在连接解析之前"这一时序约束）。
>
> 下文为第三轮之前的原文，未做修改。

现状（`persistence/webhook_delivery/model.py:21-27`）：

```python
__tablename__ = "webhook_deliveries"
channel:      String(64)   primary_key=True
workspace_id: String(512)  primary_key=True
chat_id:      String(512)  primary_key=True
message_id:   String(1024) primary_key=True
first_seen:   DateTime(timezone=True), server_default=now()
```

键长 8448 字节（utf8mb4）> 3072。

**已决策设计**（§20.8）：

```sql
-- 设计示意（不在本阶段执行）
delivery_key CHAR(64) NOT NULL COMMENT '入站事件去重键：SHA-256(provider + connection_id + external_event_id) 的十六进制',
PRIMARY KEY (delivery_key),
-- 原始 channel / conversation / message 信息保留为普通列，仅供审计与排障
```

- `delivery_key` 由应用层在写入前计算（`sha256` 已在
  `persistence/channel_connections/sql.py:401-404` 有先例）。
- ⚠️ **对应用层逻辑的影响**：`dedupe_store.py` 的 `ON CONFLICT (channel, workspace_id, chat_id, message_id)`
  目标必须改为 `delivery_key`；`release()` 的 4 列 `DELETE` 条件同样改为 `delivery_key`
  （**推荐直接用 `delivery_key` 删除**：走原 4 列的等值条件只能利用 leftmost prefix，
  `chat_id` / `message_id` 需回表过滤，而 4 列合计 8448 字节本就无法建完整索引）。
- 🔴 **实施约束（必须在设计阶段解决）**：现有去重键用的是 `channel_name`
  而不是 `connection_id`，且去重发生在**连接解析之前**
  （`app/channels/manager.py:1634-1667` 构造键、`:1670` 调用去重；
  连接解析在 `_apply_effective_owner` / `_get_bound_identity_rejection`）。
  因此改为 `SHA256(provider + connection_id + external_event_id)` 之前，必须二选一：
  **(a)** 把连接解析前移到去重之前；**(b)** 把去重时机后移到连接解析之后。
  同时必须**保留现有 fail-closed 语义** —— 拿不到 workspace / 连接标识时**跳过去重**，
  而不是把不同 workspace 的消息误判为重复（`manager.py:1653-1666` 的注释写明了这个理由）。
- ⚠️ **哈希碰撞与规范化**：SHA-256 的碰撞概率可忽略；但**必须在应用层保证规范化**
  （例如统一 trim / 大小写），否则语义上相同的两条事件会得到不同的 key。

### 15.7 本阶段**不做**的索引工作（第五轮新增）

> **决策：本次只保证"迁移后必要索引与并发语义正确"，不做任何与迁移无关的索引优化。**

| 不做的事 | 为什么不做 | 什么时候做 |
| --- | --- | --- |
| **冗余索引清理** | 需要**真实 workload** 才能判断"哪个索引没人用"；迁移期删索引会把"迁移引入的问题"与"索引变更引入的问题"混在一起，**失败归因不可能** | MySQL 上线后，用 `performance_schema.table_io_waits_summary_by_index_usage` + 真实负载 |
| **query tuning** | 同上；且 **PG 与 MySQL 的优化器行为不同，PG 上的结论不可迁移** | 上线后按 `EXPLAIN ANALYZE` |
| **索引合并**（多个单列索引 → 复合索引） | 会改变 `with_for_update` 的**锁范围** —— 这是**并发语义**，不是性能问题（InnoDB 缺合适索引即扩大锁范围，§13.2） | 上线后单独评估，且**必须配并发回归测试** |
| **根据推测 workload 新增优化索引** | "推测的 workload"在数据库替换场景里几乎总是错的 | 上线后按真实慢查询 |
| 覆盖索引 / 索引下推调优 | 同上 | 同上 |

**本阶段只做两类索引动作**（其余一律不做）：

1. **等价迁移**：现有 **47 个**索引按 `uk_` / `idx_` 命名规范重建（§15.1），**索引集合不变**；
2. **能力补偿**：为 MySQL 缺失的能力补索引 —— 即 partial unique index 的生成列替代（§15.3），
   以及 FK 移除后必须显式建立的 relationship 索引（§15.4）。
   这两类是**正确性必需**，**不是优化**。

> ⚠️ **§15.4 的定性必须收紧**：其"需要新增的索引"清单（原 7 项 → 5 项 → 实际 4 项）
> **必须逐条回答"不建它，哪条 SQL 会失去正确性保证"**；
> **答不上来的一律不做** —— 它属于"性能优化"，归入本节的不做清单。
>
> ⚠️ **§15.5 的结论保持不变**："**不建议删除任何索引**"。
> 本轮**保持该结论**，并把"是否删除冗余索引"整体**推迟到上线后**。

---

## 16. Third-party Library Compatibility

> 🔴 **第五轮范围修正**：本节原先同时评估 **CheckpointSaver** 与 **Store** 两类第三方实现。
> **Store 整体不迁移（§9）** ⇒ 本节**只评估 CheckpointSaver**；
> 原 `langgraph-checkpoint-mysql` 的 **Store 部分**（`store.value JSON`、
> `json_extract(value, …)` 前缀搜索）**不再作为决策依据**（仅保留为"该包不合规"的旁证）。

### 16.1 判定表

| 组件 | 版本 | 是否"支持 MySQL" | 是否满足本项目 MySQL 规范 | 判定 |
| --- | --- | --- | --- | --- |
| `langgraph-checkpoint-postgres` | **3.1.1**（已安装） | ❌ PG 专用 | ❌ | **不适用**（当前实现，迁移时移除） |
| `langgraph-checkpoint-sqlite` | **3.1.1**（已安装） | ❌ | ❌ | 不适用 |
| **`langgraph-checkpoint-mysql`** | **3.0.0**（PyPI 实测） | ✅ 支持（提供 `aiomysql`/`asyncmy`/`pymysql` extra） | ❌ **不满足** | **需要定制 / 不兼容当前 Schema 规范**。⚠️ 第五轮：**只用它的 saver 部分作判据**；Store 部分已移出范围 |
| `sqlalchemy` | **2.0.49** | ✅ 官方 MySQL 方言 | 取决于我们写的 DDL | 可用。⚠️ 但 `UPDATE … RETURNING` 在 MySQL 方言下**不报错地**编译出非法 SQL（§10.2 #17） |
| `alembic` | **1.18.4** | ✅ | 取决于我们写的 revision | 可用 |
| `aiobotocore` / `botocore` | 已安装（Phase 5 引入） | — | — | 对象存储可用（方案 C 的基础） |
| `asyncpg` | **0.31.0** | ❌ PG 专用 | — | 迁移时移除 |
| `psycopg` / `psycopg-pool` | **3.3.3** / **3.3.0** | ❌ PG 专用 | — | 迁移时移除 |
| MySQL 驱动 | — | — | — | **需新增**（§20 Q3） |

### 16.2 `langgraph-checkpoint-mysql` 3.0.0 违规明细

| 规范项 | 该包的实现 | 违规 |
| --- | --- | --- |
| 禁止 `JSON` | `checkpoints.checkpoint JSON NOT NULL`、`checkpoints.metadata JSON NOT NULL`、`store.value JSON NOT NULL` | ❌ |
| 禁止 `BLOB`/`LONGBLOB` | `checkpoint_blobs.blob LONGBLOB`、`checkpoint_writes.blob LONGBLOB NOT NULL` | ❌ |
| 禁止 specialized types | `checkpoint_ns_hash BINARY(16) AS (UNHEX(MD5(checkpoint_ns))) STORED` | ❌ |
| 表名 `ag_` 前缀 | 无前缀 | ❌ |
| 禁止 FK | 无 FK | ✅ |
| 至少一个 PK | 每表都有 | ✅ |
| 中文 COMMENT | 无任何 COMMENT | ❌ |
| 索引命名 `uk_`/`idx_` | `checkpoints_thread_id_idx` 等 | ❌ |
| 禁止数据库专有业务逻辑 | 大量 `json_table`/`json_keys`/`json_extract`/`json_unquote`/`json_arrayagg`/`json_contains`/`UNHEX(MD5())`/`to_base64` | ❌ |
| 官方支持声明 | **第三方**（作者 Theodore Ni），非 LangChain 官方 | ⚠️ 必须明确标注 |

### 16.3 其它需要注意的第三方耦合

| 组件 | 耦合点 | 迁移影响 |
| --- | --- | --- |
| `langgraph-api` 0.10.0 | Gateway 内嵌 LangGraph 运行时，通过 `checkpointer` 抽象使用 saver | 只要实现 `BaseCheckpointSaver` 契约即可替换 |
| `langgraph-runtime-inmem` 0.30.0 | Studio 的 pre-runtime persistence repair 依赖其 store 生命周期 | ⚠️ **第五轮**：Store 被删除 ⇒ **需复核该修复逻辑是否因此失效或可一并移除**（属 Store 删除的连带改动，见 §9.3） |
| `deerflow/checkpoint_patches.py` | 猴子补丁 `InMemorySaver.get_delta_channel_history` 与 `BinaryOperatorAggregate.update`，**已做版本守卫**（`_PATCH_VALIDATED_LANGGRAPH_VERSION = Version("1.2.9")`） | 与数据库无关，不受影响 |
| `app/gateway/auth/repositories/sqlite.py:36-39` | 依赖 **SQLAlchemy asyncpg 方言**包装后的异常形状（`AsyncAdapt_asyncpg_dbapi.IntegrityError`，见 `sqlalchemy/dialects/postgresql/asyncpg.py::_handle_exception`） | 换驱动后该判定逻辑必须重写 |
| `community/aio_sandbox/network_proxy.py:198,203` | `INSERT INTO grants(…) ON CONFLICT(host, port) DO UPDATE …` | **沙箱本地 SQLite 表**，与数据库后端无关，**不在迁移范围** |

---

## 17. Migration Risk

### 17.1 三大风险（本任务要求）

#### 风险 1：Checkpoint 持久化层没有合规实现（**最高**）

- **事实**：官方无 MySQL saver（`.venv` 实测只有 base/memory/postgres/serde/sqlite）；
  第三方包用 `JSON` + `LONGBLOB` + `BINARY(16)`；
  自研需重写 `SELECT_SQL`（含 `jsonb_each_text` / `array_agg` / `::bytea`）、
  `UPSERT_*`（含 `ON CONFLICT`）、DeltaChannel 两阶段动态列 SQL、以及 `setup()` 迁移链。
- **为什么危险**：这是**唯一可能推翻整个方案**的点。
  - 若 blob 采用方案 C（对象存储），写入路径从 1 次 DB 提交变成"对象存储写 + DB 提交"，
    必须处理部分失败与孤儿 GC，且 checkpoint 的唯一真相从"单库事务"变成"跨系统最终一致"。
    —— **已决策的缓解**：写入顺序固定为"先写对象 → 再写 DB 引用 → commit"，
    commit 失败允许留 orphan 由 GC 清理（§20.5），避免出现"Checkpoint 在、Blob 不在"的不可恢复状态。
  - 若采用 `TEXT`，则 64KB 上限与累积型 `messages` 通道直接冲突；
    放宽到 `MEDIUMTEXT` 是**已决策**的允许路径（仅限 checkpoint payload 列），
    但 `LONGTEXT` 不采用，且阈值由 `checkpoint.inline_blob_threshold` 配置化。
- **缓解**：先做 **Checkpoint 层的可替换接口 + 最小可用 MySQL 实现**，
  用现有测试回归（`tests/test_delta_channel_checkpointers.py`、
  `tests/test_run_worker_delta_resume.py`、`tests/test_multi_worker_run_ownership.py`），
  再决定是否继续。
- ⚠️ **本风险不能再靠 Redis 缓存缓解（本轮修正）**：
  `delta + Redis checkpoint cache` 已决策**不与数据库迁移同时切换**（§20.12 R1），
  因此迁移期**必须按"无缓存"评估 MySQL saver 的性能是否达标**。
  换句话说，本风险的等级在迁移期**保持"极高"**，
  而不是像上一轮暗示的那样"因为能接缓存所以降一档"。

#### 风险 2：并发语义变化（**高**）

- **事实**（第三轮后）：**2 处事务级 advisory lock** + 1 组会话级 advisory lock、
  **4 处 `RETURNING`**、**3 处 partial index**、
  1 处 PG 专有行锁强度假设（`mcp_tasks/sql.py:165-168`）、**51 处 `FOR UPDATE`**、
  默认隔离级别从 RC 变 RR。
  （~~3 处事务级 advisory lock / 5 处 `RETURNING` / 1 处 `ON CONFLICT … WHERE` /
  4 处 partial index~~ ← 渠道删除后的缩减项）
- **为什么危险**：这些是"能跑但可能静默错误"的一类问题。最典型的静默错误场景：
  - ~~`dedupe_store` 的 TTL 重入判定在 MySQL 上被简化后，过期的 webhook
    不再被重新接纳（**重复消息被永久丢弃**）~~ ⛔ **该场景随渠道删除消失**；
  - `scheduled_task_runs` 的 `occurrence_seq` 分配改用 `LAST_INSERT_ID(expr)` 后
    与 `run_events` 的 `AUTO_INCREMENT` 语义互相污染（**序号串号**）；
  - RR 下的 gap lock 让 claim 语句的"跳过"行为与 PG 不同，导致偶发死锁（`1213`）而非正确的跳过；
  - **`UPDATE … RETURNING` 在 SQLAlchemy 下编译期不报错**，
    意味着现有测试（包括断言编译 SQL 的测试）**不会**捕获这个错误。
- **缓解**：为每个并发原语写 **MySQL 专属的并发回归测试**；
  显式 `SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED`；
  用 `scripts/benchmark/concurrency/worker.py` 做双 Worker 压测；
  对 **4 处** `RETURNING`（第三轮后；原 5 处）加**静态检查**
  （例如在 CI 里对 MySQL 方言编译并断言不含 `RETURNING`）。

#### 风险 3：Schema 层的硬约束（**中**；第三轮后已无硬阻塞）

- **事实**：
  1. ✅ ~~🔴 **`webhook_deliveries` 主键 8448 字节 > 3072**，按现状**建表就失败**。~~
     **第三轮后该表已删除，本项消失** —— 缩减后的 15 张表最大唯一键
     1788 字节（`mcp_tasks`），距上限有 42% 余量（§4.7）。
     因此风险 3 由"**中高（含硬阻塞）**"降为"**中**（仅剩设计核对项）"。
  2. InnoDB 索引键长上限 3072 字节（utf8mb4 下单列 ≤ 768 字符）——
     `checkpoint_ns` 会因嵌套子图增长，必须**实测并断言**真实最大长度
     （§20.11 V2，是唯一保留的待实测项之一）。
  3. `TEXT` 不能作主键 → `ag_checkpoint*` 的 5 个主键列必须收紧为 `VARCHAR(N)`。
  4. `DATETIME` 默认精度 0 → **截断亚秒**，破坏 `scheduled_task_runs` 的 FIFO
     排序与 `expire_queued_runs` 的过期判定（**48 个时间列**受影响；第三轮后，原 61）。
  5. `DateTime(timezone=True)` 在 MySQL 上**静默丢弃时区**。
  6. `TEXT` 上限 64KB 与 checkpoint blob 的体积冲突。
- **为什么危险**：这类问题在开发环境（小数据、单进程、UTC 单时区）**不会暴露**，
  只会在生产（长会话、多 Worker、跨时区）暴露。
- **缓解**（均已决策）：所有时间列显式 `DATETIME(6)` + 应用层统一 UTC 写入边界（§20.10 #1）；
  `checkpoint_ns` 先实测再定长并**加运行时断言**（§20.11 V2）。
  ~~`webhook_deliveries` 的主键改造必须先于其他工作（§20.8）~~
  ⛔ **第三轮后随表删除作废**。
  ⚠️ **第四轮补充**：本风险的"组织侧缓解"还包括一条 ——
  **`0001_mysql_baseline` 必须一次写全裁剪后的 Schema**（§20.2），
  因此它的 review 是本风险最集中的一道闸门。

### 17.2 风险清单（按严重度）

| # | 风险 | 严重度 | 可能性 | 缓解 |
| --- | --- | --- | --- | --- |
| 1 | Checkpoint 自研 saver 的功能/性能不达标 | 极高 | 中 | 分阶段验证；保留 PG 回退能力 |
| 2 | checkpoint blob 超出 TEXT 上限 | 高 | 高 | 方案 C（对象存储，端口已存在）或明确放宽到 `MEDIUMTEXT` |
| ~~3~~ | ~~**`webhook_deliveries` 主键超限导致建表失败**~~ | ✅ **已移除** | — | 第三轮删渠道后该表已删除；缩减后 **15 张表无任何越界键**（§4.7） |
| 4 | `UPDATE … RETURNING` 编译期不报错 → 运行期才失败 | 高 | **高** | CI 静态检查（MySQL 方言编译 + 断言无 `RETURNING`）+ 逐处手工改写 |
| 5 | advisory lock 替换后调度器重复触发/漏触发 | 高 | 中 | 锁表 sentinel + 并发回归测试 |
| 6 | `RETURNING` 替换后序号重复/跳号（`LAST_INSERT_ID` 污染） | 高 | 中 | 两步法（`FOR UPDATE` + `UPDATE`）+ 唯一约束兜底；**禁用 `LAST_INSERT_ID(expr)`** |
| 7 | RR 隔离级别导致死锁率上升 | 中高 | 高 | 显式 RC + 锁顺序审计 + 死锁重试（识别 1213） |
| 8 | 时间精度截断导致 FIFO/过期判定错误 | 中高 | 高 | `DATETIME(6)` + 边界转换 + 测试 |
| 9 | partial index 生成列 workaround 的锁/写放大开销 | 中 | 中 | 压测写入路径 |
| 10 | **20 张表**（15 应用 + 5 框架；第五轮后，原 26）重命名遗漏<br>~~bootstrap 常量（`_CANONICAL_0019_SCHEMA_FLOOR` 硬编码 274 个列名）~~ ⚠️ **第四轮**：该常量**不再为 MySQL 服务**（legacy，§7.8.4），**不再是风险面** | 中 | 中 | 启动期断言 + 一次性 grep 清单 + 单测 pin（已有 `test_baseline_table_names_constant_matches_0001` 等模式可复用） |
| 11 | 级联删除从 FK 移到应用层后出现孤儿行 | 中 | 中 | 显式级联实现 + 孤儿巡检脚本 |
| 12 | `mcp_tasks` 的 `FOR SHARE` 归属校验在 MySQL 上失去正确性论证 | 中高 | 中 | 重新设计为"生成列唯一约束 + 乐观栅栏"，并写并发测试 |
| 13 | 🔻 **113 个**命中 `postgres` 的测试文件需要系统改造（第五轮重算；原记 111） | 中 | 高 | 分批：先加 `mysql` 方言参数化，再逐步删除 PG 专属断言 |
| 14 | 移除 PG 依赖后 Helm/文档/CI 不一致 | 低 | 高 | 文档与 chart 同步更新 |
| 15 | **`run_events.seq` 在 MySQL + RC 下失去串行化**（`db.py:153` 的 `else` 分支实为 SQLite 语义，且**无重试**） | 中高 | 中 | **V3 实测**（§13.7）；改用 per-thread sentinel row + `SELECT … FOR UPDATE`，或唯一约束 + bounded retry |
| 16 | **Redis 重启后 SSE 静默丢失事件**（gap 检测不覆盖"丢流"；SSE 路径无 RunEventStore 自动补偿） | 中 | 中 | **V4 实测**（§18.1）；补 durable replay fallback 或明确客户端 reload 恢复契约 |
| **17** | 🔴 **删除 Store 时漏改耦合点** —— 3 处非显然耦合：`_sqlite_utils` 被 checkpointer/health 复用、`MemoryThreadMetaStore` 依赖 `BaseStore`、`deps.py:435` 的**无条件构造** + 5 处 `store=` 管线 + `app.py` 孤儿迁移 | 中 | **中高** | 按 **§9.3 的 11 项清单**逐项改；改完跑 Gateway 启动 smoke test + checkpoint 回归 |
| **18** | 🔴 **只改驱动、不改 `("sqlite","postgres")` 守卫** ⇒ 进程**直接 `SystemExit` / `ValueError`**（不报 SQL 错误，而是拒绝启动） | **中高** | **高** | 按 **§1.7 D 第 12 项**的 **7 处**清单全部扩展 `mysql`；启动 smoke test 必须覆盖 4 条守卫路径（`scheduler.multi_instance` / `GATEWAY_WORKERS` / `agent_storage` / `subagent_batches`） |
| **19** | 🔴 **误把"同步 Saver 不实现"扩展成"同步驱动也不要"** ⇒ `agent_storage.backend: db` 整链（agent / managed-subagent 定义读写）失效 | **中高** | 中 | 按 **§8.6（3）**区分二者：**保留同步驱动，只去掉同步 Saver**；并覆盖"图子进程构建 agent"这条路径的测试 |

---

## 18. Recommended Target Database Boundary

**基于代码调查结果的推荐边界**（与任务给出的候选方向基本一致，
但每一项都由现状推导，而非预设）：

```
Agent Runtime
│
├── MySQL 8.0.24
│   ├── Application Data        ag_agents / ag_projects / ag_users / ag_user_preferences /
│   │                           ag_feedback / ag_personal_access_tokens /
│   │                           ag_managed_subagents / ag_subagent_batches /
│   │                           ag_subagent_batch_items
│   │                           （第三轮已删除 ag_channel_* 4 张）
│   ├── Thread / Run            ag_threads_meta / ag_runs / ag_run_events
│   ├── Checkpoint              ag_checkpoint / ag_checkpoint_blob / ag_checkpoint_write /
│   │                           ag_checkpoint_migration
│   │                           （blob = "小内联 MEDIUMTEXT(base64) + 大对象引用"混合；
│   │                             阈值由 checkpoint.inline_blob_threshold 配置，不写死）
│   ├── ~~Memory Metadata         ag_store（LangGraph Store，仅 agent 文档）~~
│   │                           ⛔ 第五轮删除：Store 不迁移（§9）；thread 元数据改由
│   │                              ThreadMetaRepository（`threads_meta` 表）承载
│   └── Operational Data        ag_scheduled_tasks / ag_scheduled_task_runs / ag_mcp_tasks
│                               （第三轮已删除 ag_webhook_deliveries）
│
├── Object Storage（✅ HEAD 已落地 S3/MinIO 端口）
│   ├── Artifacts               thread outputs / .tool-results（已接入）
│   ├── Uploads                 已接入
│   └── Large Payload           **checkpoint 大 blob**（需新增命名空间）、
│                               mcp_tasks.result_artifact、subagent_batch_items.result
│
├── Knowledge Service
│   └── Vector / RAG            当前已是外部 RAGFlow HTTP；本地检索走 SQLite FTS5
│                               （deermem / task_continuity）—— **不进 MySQL**
│
└── Remote Sandbox
    └── Temporary Workspace     LocalSandboxProvider / E2B / boxlite / tenki / opensandbox
```

**推导依据**

1. **Checkpoint 进 MySQL 是可行的，但必须自研 + 混合 blob 策略**
   （因为规范禁止 BLOB 且 `TEXT` 有 64KB 上限，而 `messages` 通道是累积 reducer）。
2. **Vector 不进 MySQL**：现状已经满足（pgvector 未启用），
   不应为了"统一数据库"而引入。
3. **Object Storage 是必需的，不是可选的**：它是解决 checkpoint blob 与 `TEXT` 上限
   冲突的**唯一合规路径**。而 HEAD 已经提供了这个端口，成本已大幅下降。
4. **不需要引入 Redis 来替代 advisory lock**：Redis 已存在于项目
   （`stream_bridge` / `checkpoint_cache` / dev compose），但它**不能**替代数据库级事务锁语义；
   若采用"锁表"方案可以完全不引入新依赖。
   （`docs/architecture/redis-checkpoint-store-feasibility.md` 已单独评估并给出
   "技术上可行、工程上不划算"的结论，本文档不重复。）
5. **不需要为 `webhook_deliveries` 引入 Redis 或专用去重服务**：
   `delivery_key CHAR(64)` 短哈希主键 + 条件 upsert 重构即可满足规范（§20.8）。
   —— 注：这是在**默认方案**下的结论。若把 Redis 当作 volatile 缓存，
   还存在一条"整表移出 MySQL"的可选降级路径，代价是去重变成有损，
   权衡见 **§18.4**（建议保留表方案为默认）。
6. **Channel 边界要收敛，不是全部保留**：Channel Core 只含
   Connection + Conversation Mapping + Inbound Event Deduplication；
   `channel_credentials` 长期并入 Credential Broker，`channel_oauth_states` 属 OAuth Provisioning（§20.8）。
7. **终态是单后端**：PostgreSQL 实现只在迁移期作为 rollback path 存在，
   稳定后删除 —— 不长期维护 `postgres | mysql` 双后端（§20.6）。

### 18.1 现有 Redis 设施盘点（三处，**性质各不相同**）

在讨论"再往 Redis 里加什么"之前，必须先分清现有三处 Redis 用途的性质 ——
因为前提是"**Redis 只当缓存、不持久化、重启丢数据**"，而这个前提**并非对三处都成立**。

| # | 位置 | 用途 | 丢了会怎样 | 与"volatile Redis"前提是否兼容 |
| --- | --- | --- | --- | --- |
| 1 | `runtime/checkpoint_cache/`（`base.py` / `memory.py` / `redis.py` / `provider.py`） | checkpoint **delta 历史**读穿缓存 | 回源查库重算 | ✅ **完全兼容** —— 契约明写条目**不可变**（append-only lineage），正确性从不依赖失效；`ttl_seconds` 只是泄漏兜底（`CheckpointCacheConfig` 文档："a leak safety net, not a correctness mechanism"） |
| 2 | `runtime/stream_bridge/redis.py` | worker→SSE 的生产/消费桥，支持 `Last-Event-ID` 重放 | ⚠️ **见下方 V4 —— 不能假定"`run_events` 会自动回源"**。已确认：gap 检测**只覆盖"保留窗口被裁掉"**，**不覆盖"Redis 重启丢流"**；且 SSE 路径**没有** RunEventStore 自动补偿 | ⚠️ **部分兼容**：`run_events` 中数据确实还在（durable），但**订阅端不会自动补偿**，需要显式恢复契约 |
| 3 | `community/aio_sandbox/ownership/redis.py` | 多实例**沙箱容器所有权租约**（`own:` / `del:` + TTL，全部经 Lua 保证原子） | **所有权状态全丢** → 每个实例都认为容器无主 → `claim` 成功 → 一个实例 adopt 另一个实例正在用的容器并随后 idle-destroy | 🔴 **不兼容** —— 这正是 issue #4206 要修的问题。该 store 是**真相源（lease）而非缓存** |

**结论：如果 Redis 被规划为"重启即空"，第 3 处（沙箱 ownership）必须排除在同一个实例之外**，
或者给它单独的持久化配置。这一点不是本文档的迁移范围，但**是"Redis 只当缓存"这个前提的边界**，
需要在部署设计里显式区分，否则会把一个已修好的多实例 bug 重新引入。

**另外两条事实**（决定了后面能做什么）：

- 现有 Redis 用法**全部是普通命令**（`SET` / `GET` / `EXPIRE` / `PIPELINE` / `DELETE` + ownership 的 Lua），
  全仓 **没有任何 Redis Stack 模块命令**（`JSON.*` / `FT.*` / `TS.*` / `BF.*` 零命中）。
- `checkpoint_cache` 当前**未被启用**：`config.yaml` 只配了 `stream_bridge.type: redis`，
  而 `database.checkpoint_cache.type` 默认是 `memory`（进程内 LRU，128 条）；
  且 `CachedHistorySaver` **仅在 `checkpoint_channel_mode == "delta"` 时挂载**
  （`async_provider.py:242-253`），当前配置是 `full` → **缓存当前完全没生效**。

#### V4 — Redis StreamBridge 重启 / `StreamGap` 后的 SSE 恢复路径（**待实测确认**）

> **V4 — Redis StreamBridge 重启 / `StreamGap` 后，当前 SSE 路径是否存在 RunEventStore 自动补偿。**

**结论（代码层面已确认，不需要实测即可判定"没有自动补偿"）**：
**SSE 路径上没有 RunEventStore 自动补偿。** 恢复是**客户端驱动**的，且存在一个未被覆盖的空洞。
证据链：

| 环节 | 代码事实 | 含义 |
| --- | --- | --- |
| 事件 id 的来源 | `sse_consumer` 用 `format_sse(entry.event, entry.data, event_id=entry.id)`，而 `entry.id` 是 **Redis stream entry ID**（`redis.py:133-142`） | 客户端回传的 `Last-Event-ID` 是 `1737…-0` 这种**Redis 格式**，不是 DB 的 `seq` |
| 终态 run 的流缺失 | `_terminal_record_stream_missing`（`services.py:231-246`）**只对终态 run** 生效；命中时创建端返回 `gap` 帧、其余端返回 `end` | 只覆盖"run 已结束且流没了" |
| gap 帧的恢复语义 | gap 帧内容为 `{"code":"stream_replay_gap", …, "recovery":"reload_durable_state"}`（`services.py:1943-1953, 1966-1976`） | **恢复动作是让客户端自己重载 durable state** —— 服务端不做补偿 |
| **gap 检测的触发条件** | `redis.py:291`：`if earliest_entries and (gap_detection_enabled or …)` → **必须 `earliest_entries` 非空** | 🔴 **空洞所在**：Redis 重启后流**被整条删除**，`XRANGE` 返回空 → `earliest_entries` 为空 → **gap 检查被跳过** |
| 跳过之后的行为 | `redis.py:314-357`：无数据 → 落入阻塞 `XREAD`，用 `stream_id` 继续等新事件 | 新事件照常返回 → **订阅端静默地从新流继续，没有任何 gap 信号** |

**因此 gap 检测的真实覆盖面是**：
✅ "流还在，但游标已早于 `XRANGE` 可见的最早条目"（**maxlen 裁剪 / TTL 过期**，即 §18.1 提到的保留窗口丢失）
❌ **"流被整条删除"**（Redis 重启且未持久化 —— 正是本次讨论的前提）
❌ **"`XREAD` 期间 Redis 短暂不可用导致中间事件未投递"**

**这也说明不能用 `run_events` 的存在来论证"自动回源"**：
`run_events` 里确实有完整数据（durable），但**没有任何代码路径在 gap 之后去读它**。

**待办（二选一，必须显式决定）**：
1. **补 durable replay fallback**：在检测到"流不存在 / 游标早于保留窗口"时，
   由服务端从 `RunEventStore` 按 `seq` 补齐并推送（需要把 SSE 的 `event_id`
   从 Redis stream ID 改为可回源到 DB 的标识，这是一处**接口级改动**）；
2. **明确客户端恢复契约**：规定客户端在 `gap` 帧、或 **SSE 连接意外结束**时
   必须重新拉取 history/messages 接口做全量重放 ——
   但**前提是 gap 检测本身要补上"丢流"这一格**（即 `earliest_entries` 为空且
   游标非 `0-0` 时也应判定为 gap），否则客户端**根本收不到需要恢复的信号**。

> **归类**：这是**可靠性/可用性**问题，**不是 MySQL 迁移阻塞项**。
> 但它与本次迁移**有交集**：迁移后若继续用 Redis StreamBridge，
> 该空洞在"Redis 被当作纯缓存、随时可重启"的前提下会**更容易被触发**。
> 因此建议在迁移的**同一批次里**至少把 gap 检测的空洞补上（改动很小：一个条件判断），
> 把完整的 durable replay fallback 留作后续项。

### 18.2 三类判断：能帮 / 不能帮 / 看似能帮但不能用

用户的问题是"哪些接入 Redis 能**降低 MySQL 的实现难度**、提高效果"。
这里必须把两类收益拆开，因为它们的性质完全不同：

**(A) Redis 完全帮不上的部分 —— MySQL 的 schema 层硬约束（占迁移难度的大头）**

| MySQL 侧的难点 | Redis 能否减轻 | 为什么 |
| --- | --- | --- |
| `TEXT` 64KB 与 checkpoint blob 冲突 | ❌ 完全不能 | 这是"字节存不进列"的问题，只能靠 `MEDIUMTEXT` / 对象存储（§20.5）解决 |
| **4 处** `RETURNING` 无等价物（第三轮后；原 5） | ❌ | SQL 语义缺失，与缓存无关 |
| **3 处** partial unique index（第三轮后；原 4） | ❌ | DDL 能力缺失 → 生成列 workaround |
| **2 处** FK 移除 + 应用层级联（第三轮后；原 4） | ❌ | 约束语义问题 |
| ~~`webhook_deliveries` 主键 8448 > 3072~~ | ✅ **第三轮已消除** | ~~键长是 DDL 限制~~ → 表已删除（原可考虑表级移出，见 §18.4） |
| **48 个** `DateTime(timezone=True)` → `DATETIME(6)` + UTC（第三轮后；原 61） | ❌ | 类型映射问题 |
| 中文 COMMENT / `ag_` 前缀 / `uk_`·`idx_` 命名 | ❌ | 纯 DDL 规范 |
| `checkpoint_ns` 定长（V2 待实测） | ❌ | 主键列定义问题 |

> **一句话：Redis 缓存降低的是"读路径成本"，不是"schema 迁移难度"。**
> 迁移难度的主体（自研 saver 的 DDL/聚合/UPSERT、并发原语替换、schema 硬约束）
> **不会因为加了 Redis 而减少一行**。这个判断必须先说清楚，否则很容易高估缓存的价值。

**(B) Redis 能真正改善的部分（读路径 / 热点 / 多实例）**

| 候选 | 现状证据 | 收益 | 失效代价（重启丢数据） | 需要失效逻辑？ |
| --- | --- | --- | --- | --- |
| **checkpoint delta 历史缓存** | **已实现**，`CachedHistorySaver` 是 **任意 `BaseCheckpointSaver` 的读穿包装**（`cached_saver.py` 开头文档） | 直接压掉 checkpoint 读路径上最贵的聚合查询 —— 也就是**最难那一项的运行时成本** | 回源查库，无正确性影响 | **不需要**（条目不可变）<br>⚠️ 但**迁移期不启用**：见 §18.3 **第 0 档**（已决策：MySQL Saver 稳定后再作为独立变更评估） |
| **鉴权：用户行 / PAT 摘要查询** | 每请求 `cookie → decode JWT → DB lookup → token_version match`（`langgraph_auth.py:68-101`、`deps.py:816`）；PAT 走 `pat_repo.get_active_by_digest`（`pat.py:187`） | 削掉**每请求一次**的 DB 读；API 型负载下这是最高频的读 | 回源查库 | **需要**（见下方风险） |
| **渠道会话映射** | `channel_connections/sql.py:508-546` 按 `(connection_id, external_conversation_id, external_topic_id)` 查/建映射，每条入站消息都走（`manager.py:1838`） | 每条入站消息少一次 DB 往返 | 回源查库 | 需要（映射会被 upsert） |
| **线程列表 / run 列表页** | `thread_meta/sql.py:230,247,261` 的 `json_match` 过滤 | 降低 UI 列表的库压力 | 回源查库 | 需要，且**写入面很宽**（每个 thread 变更都要清） |

**(C) 看似是缓存、实际是真相源 —— 不能用 volatile Redis**

**Redis 的职责边界（已决策，固定不变）**：

> **Redis 只允许承载"丢失后能够从 durable store 重建"的状态。**
> **任何 durable truth 都不能以 volatile Redis 作为唯一真相源。**

按此边界逐项列出**明确排除**的候选（与项目负责人给定的清单逐条对齐）：

| # | 候选（按职责命名） | 为什么不能 | 证据 |
| --- | --- | --- | --- |
| 1 | **sequence allocation**（`run_events.seq` 分配） | 序号是**排序真相源**；Redis 丢了会跳号/重号，直接破坏 `uq_events_thread_seq` | `events/store/db.py:133-153`（另见 §13.7：该处**在 MySQL 上本身也还没验证**） |
| 2 | **run ownership**（`runs` 的 worker 租约 / 取消请求） | 租约是**所有权真相源**；丢失 → 两个 worker 同时认为拥有同一个 run | `run/model.py:51-56`、`run/sql.py:563,594,627` |
| 3 | **scheduler lease / due queue**（`scheduled_tasks`、`scheduled_task_runs` 的到期与认领） | 到期时间是**调度真相源**；丢失 → 漏触发或重复触发 | `scheduled_tasks/model.py:27`、`scheduled_task_runs/sql.py:754-763` |
| 4 | **MCP lease**（`mcp_tasks` 的 poll / cancel / notification claim + lease） | 同上（租约 + `dispatch_version` 乐观栅栏） | `mcp_tasks/model.py:51`、`mcp_tasks/sql.py:230,376,480` |
| 5 | **durable task claim**（`subagent_batch_items` 的认领状态） | 认领是**唯一真相源**；丢失 → 同一 item 被两个 worker 领取 | `subagent_batches/model.py:54-55,72` |
| 6 | **sandbox ownership**（沙箱容器所有权租约） | 见 §18.1 第 3 行 —— **项目里已经存在**，且**与"volatile Redis"前提直接冲突** | `aio_sandbox/ownership/redis.py` |
| 7 | **advisory lock 的替代** | 锁不是缓存；且 §18 推导依据 #4 已明确否决（Redis 不能替代数据库级事务锁语义） | `docs/architecture/redis-checkpoint-store-feasibility.md` |
| ~~8~~ | ~~**inbound dedupe 的接纳判定**~~ | ⛔ **第三轮后本项消失** —— 去重能力本身随渠道删除；未来 Custom Channel 若重建入站去重，本条判据**仍然适用**（去重是正确性边界，不是性能） | ~~`app/channels/dedupe_store.py`、`manager.py:1634-1667`~~ |

> **判据只有一条**：**这份数据丢了之后，系统是否仍能自行重建出正确结果？**
> 能 → 可以放 volatile Redis；不能 → 必须留在 MySQL（或给 Redis 单独开持久化）。
> 上表剩余 7 项每一项都答"**不能**"。
>
> ⚠️ 注意 #6（sandbox ownership）**本身就已经跑在 Redis 上**，
> 它的问题不是"要不要搬过去"，而是"**已经在那里了，且前提冲突**"。
> 该项的持久化方案**本阶段不决策**（§20.13）。
>
> 注意第 6 行与第 8 行的**性质不同**：第 6 行是"**已经**跑在 Redis 上且前提冲突"，
> 需要在部署设计里**显式隔离**（见 §18.1 结论）；第 8 行是"**要不要搬过去**"的选项，
> 默认**不搬**（§18.4 列为可选降级路径）。

### 18.3 建议接入的缓存（按收益 / 风险排序）

> 🔴 **已决策的前置约束：Redis checkpoint cache 不与数据库迁移同时切换。**
>
> **第一阶段保持 `checkpoint_channel_mode = full` 完成 PostgreSQL → MySQL 的 checkpoint 迁移；
> MySQL Saver 稳定后，再单独评估 `delta + Redis checkpoint cache`。**
> **不要为了利用 Redis cache 而同时改变 checkpoint 存储语义。**
>
> 理由：`full` → `delta` 改变的是**checkpoint 的存储与读取语义本身**（不是纯性能开关），
> 把它与"换数据库"叠在同一个变更窗口里，会让**失败归因不可能**——
> 一旦出现 checkpoint 读取异常，无法区分是"MySQL saver 实现问题"还是"delta 模式语义问题"。
> 因此这两件事必须**串行**，不能并行。

**第 0 档：本阶段（MySQL fresh-cutover）完全移出范围 —— 第五轮定稿**

> 🔴 **第五轮定稿：本档不是"迁移期先不做"，而是"本次变更范围里根本不设计"。**
>
> 具体含义（这四条是**禁令**，不是建议）：
>
> 1. **保持 `checkpoint_channel_mode = full`。** 本阶段**不切 `delta`**，
>    不评估、不试跑、不做灰度。
> 2. **不启用 Redis checkpoint cache。** `database.checkpoint_cache.type` 保持默认
>    （即 `memory`，等价于不生效），本阶段不新增任何 `checkpoint_cache` 相关配置项。
> 3. **不为"未来的 Redis cache"预置任何设计。** 不在 MySQL Saver / Schema / 键空间里
>    提前引入 namespace 分层、失效钩子、级联驱逐、TTL 分级等结构。
>    **理由**：这些结构的形状取决于将来 `delta` 模式的实际访问模式，
>    现在设计等于**凭猜测给当前实现加约束**；一旦猜错，它们会变成 MySQL 侧的
>    历史包袱（正好是本次瘦身要清除的那类东西）。
> 4. **不在本阶段修改 `checkpoint_cache_db_hash()`。** 该缺口（§18.5 #1）只在
>    **真正启用** Redis cache 时才会被触发；本阶段不启用 ⇒ 不触发 ⇒ 不修。
>    它与 `delta` 一起挂到同一个后续项上。

**为什么"不修"是安全的**：`database.checkpoint_cache.type` 当前就是 `memory`
（`config.yaml` 未配置该字段，走默认值），且 `CachedHistorySaver` **只在
`checkpoint_channel_mode == "delta"` 时才被挂载**（`async_provider.py:242-253`）。
两个条件同时不成立 ⇒ 缓存层**在本阶段根本不在执行路径上**，
留着它不动不会产生任何运行时影响。

**它未来的样子（仅登记，不设计）**：`CachedHistorySaver` 是 saver-agnostic 的读穿包装
（`checkpointer/cached_saver.py`），**自研 MySQL saver 天然可以继承它**，
届时不需要为 MySQL 写任何缓存代码。这是"降低 MySQL 实现难度"里**唯一真正成立**的一条 ——
它不减少 MySQL 侧的代码量，但**把最难那一项的运行时开销搬走**，
让"自研 saver 性能是否达标"（§17.2 风险 1）在**稳定期之后**下降一档。
把它记为 **Phase 7 之后的独立性能优化项**，不进 Goal / Phase 顺序（§21.3(5)）。

**第 0 档（原始评估记录，保留备查）**

0. ~~启用 `database.checkpoint_cache.type: redis` + 把 `checkpoint_channel_mode` 切到 `delta`~~
   —— 原始结论是"在 MySQL Saver 稳定之后作为独立变更评估"。
   ⛔ **第五轮后不再作为本阶段范围，也不再在本文档中展开其配置细节。**
   原始评估中的三条前置条件仍然有效，一并挂到未来那个独立项上：
   - ⚠️ 启用前必须先修 §18.5 #1 的 `checkpoint_cache_db_hash()` 缺口（否则会跨部署撞 key）。
   - ⚠️ 切换 `checkpoint_channel_mode` 是**重启生效且全进程必须一致**的配置
     （`database_config.py` 的字段说明原文）。
   - ⚠️ **同步侧会拒绝**：`checkpointer/provider.py:182` 对同步 saver 直接抛
     `ValueError("... 'redis' is not supported on the sync checkpointer ...")`。

**第 1 档：迁移期就可以做（与 checkpoint 语义无关，收益明确）**

1. **鉴权读缓存（`users` 行 + PAT 摘要）** —— 削掉每请求一次 DB 读。
   ⚠️ **这不是纯性能缓存，带安全语义**：缓存值里含 `token_version`，
   而 `token_version` 正是"改密后使旧 JWT 失效"的机制
   （`jwt.py:18`、`langgraph_auth.py:101`、`auth.py:545`、`reset_admin.py:68`）。
   若只靠 TTL 自然过期，**改密后旧令牌会在 TTL 窗口内继续有效**。
   因此必须：在**所有**递增 `token_version` 的写路径上**显式删除该用户的缓存键**，
   并把 TTL 当作兜底而非机制（与 `checkpoint_cache` 的设计取向相反）。
2. ~~**渠道会话映射缓存** —— 每条入站消息少一次往返。键为
   `(connection_id, external_conversation_id, external_topic_id)`，在 upsert 路径上失效。~~
   ⛔ **第三轮后本项消失**（渠道删除）。第 1 档因此只剩 **auth 缓存 1 项**。

**第 2 档：收益中等 / 失效面宽，建议先不做**

3. **线程 / run 列表结果缓存** —— `thread_meta/sql.py:230,247,261` 的 `json_match` 过滤
   收益中等、但失效面很宽（thread 的 `status` / `display_name` / `pinned` / `archived` /
   `project_id` 任一变更都要清），建议**等有实际压测数据再评估**。

### 18.4 ~~唯一能"真正降低实现难度"的一项：把 webhook 去重整体移出 MySQL~~ ⛔ **已作废（第三轮）**

> ⛔ **本节结论已被第三轮范围决策取代。**
> 本节原本建议"用 Redis `SET key NX EX` 承接入站去重，从而让整张
> `webhook_deliveries` 表消失"。**第三轮直接删除了渠道，因此这张表本来就会消失**，
> 不再需要通过 Redis 绕开它 —— 本节要论证的那 5 项"消失的工作"
> （表本身、`delivery_key` 主键重设计、条件 upsert 重构、`make_interval` 改写、
> COMMENT / 索引规范补齐）**全部自然消失，且不引入任何"有损去重"的代价**。
>
> **这是第三轮相对"用 Redis 兜"方案的关键优势**：
> 同一个收益，但**不需要**业务方接受"Redis 重启后重复入站可能被处理两次"这个代价。
>
> 本节保留仅为历史记录：未来重新设计 Custom Channel 的入站去重时，
> 下面的权衡清单（有损去重 / fail-closed 语义 / 排障能力下降）**仍然完全适用**。
>
> 下文为第三轮之前的原文，未做修改。

§18 推导依据 #5 的结论是"不需要为 `webhook_deliveries` 引入 Redis"。
**但那条结论是在"Redis 只当缓存"这个新前提之外得出的**，现在值得重新权衡一次，
因为它是**唯一一处"用 Redis 可以让一整张 MySQL 表消失"**的地方：

把入站去重改成 Redis `SET key NX EX <ttl>`（key = `delivery_key`）后，**以下工作全部消失**：

- `webhook_deliveries` 表本身（**8448 字节主键超限 → 无法建表**这个确定性阻塞直接消失）；
- §20.8 的 `delivery_key CHAR(64)` 主键重设计；
- `ON CONFLICT … DO UPDATE … WHERE … RETURNING` 的条件 upsert 重构
  （`dedupe_store.py:145-162`，被 §15 标为 **High（最高）** 难度）；
- `make_interval(secs => :ttl)` 的改写与惰性 DELETE 清理；
- 该表的中文 COMMENT、索引命名、TTL 索引等一堆规范补齐。

**代价（必须由业务方明确接受，不能由实施方默认）**：

1. **去重变成有损的**：Redis 重启 → 窗口内的重投递不再被识别 → 同一入站消息可能被处理两次。
   缓解因素：下游 `runs` 有 `uq_runs_thread_active`（每线程至多一个活跃 run），
   重复入站很可能被"线程忙"挡掉而不是真的跑两次 —— **但这个兜底需要实测确认**，
   不能当作前提。且 `manager.py` 的 `release()` 语义本来就会在失败时**主动放弃**去重键，
   说明系统已容忍一定程度的"重复处理"。
2. **多实例共享去重状态的能力依赖 Redis 可用性**：Redis 不可用时需 fail-closed
   （跳过去重）还是 fail-open（拒绝入站），要显式决定；现有实现是 fail-closed
   （`manager.py:1653-1666`），这个语义**必须保留**。
3. **排障能力下降**：DB 里不再有明文 4 元组可查（§20.8 已记录这一点）。

**我的建议**：**不要为了省事而默认这么做**。理由是"去重"属于 §18.2 (C) 那一类语义 ——
它丢的不是性能而是正确性边界。可以保留"表 + 短哈希主键"的合规方案作为默认，
把"整表移出"列为**可选降级路径**：如果实施到阶段 4 时发现条件 upsert 的重构成本超出预期，
再切到 Redis 方案，并同步接受上面三条代价。

> ⚠️ **两条边界必须写清楚，否则这条"可选路径"会被当成实施者的逃生通道**：
>
> 1. **默认方案下 `webhook_deliveries` 仍然进 MySQL**，因此
>    **8448 字节主键超限仍是确定性硬阻塞（B1）**，仍必须在阶段 1 解决。
>    "整表移出"是**改变迁移范围**的决定，不是绕过 DDL 限制的取巧。
> 2. **是否接受"有损去重"是业务决策，不是实现层可以自行选择的**。
>    按 §20.12 **R3** 的原则，该表**默认留在 MySQL**；
>    要切到 Redis，必须由业务方**显式接受**重复入站可能被处理两次的后果，
>    并同时决定 Redis 不可用时的 fail-closed / fail-open 语义（现有实现是 **fail-closed**）。
>    把它当作"实施受阻时的自动降级"是**错误用法**。
>
> 另注：本项与 §20.12 **R1** 的"不与迁移同时切换缓存"**不冲突** ——
> R1 约束的是 **checkpoint cache 的启用时机**（`delta` 模式语义变更），
> 而本项是**去重职责的归属**（表在不在 MySQL 里），两者是不同层面的决定。

### 18.5 新增 Redis 缓存**反而增加**的工作量（必须一并计入）

> 🔻 **第五轮定性：本节三项**在本阶段**全部不实施**。
> 因为本阶段**不新增任何 Redis 缓存**（§18.3 第 0 档 / §20.12 R1），
> 这三项成本**都不会在当前变更里发生**。本节保留的目的是
> **防止未来"顺手加个缓存"时低估成本** —— 它们是那个独立性能优化项的**成本清单**。
> ⚠️ 换句话说：**不要**因为本节列出了"必须先补 X"，就把 X 拉进本次 MySQL 迁移。

加缓存不是纯收益，以下三项是真实成本：

1. 🔴 **`checkpoint_cache_db_hash()` 缺少 `mysql` 分支 —— 启用缓存前必须先补的稳定身份派生。**
   该函数按 `backend` 分支派生"部署身份"哈希
   （`checkpoint_cache/provider.py:44-53`）：`postgres` 用 host/port/database，
   `sqlite` 用文件路径，**其余一律落到 `else: identity = "memory"`**。
   也就是说 `backend: mysql` 会退化成**常量身份** → 两个不同 MySQL 库的部署若共用一个 Redis，
   会得到**相同的 key 前缀**；而条目按 `(prefix, thread_id, ns, checkpoint_id, channel)` 索引，
   同一 `thread_id` 在两个库中就会**互相命中对方的 checkpoint 历史**。
   这个 bug 在 `delta` 模式下才会暴露，且表现为"读到别的部署的数据"而非报错。

   **必须补齐的实现要求**（不是"加一个 `elif`"就够，要保证**跨部署唯一且跨凭据轮换稳定**）：

   | 要求 | 说明 |
   | --- | --- |
   | **派生自 MySQL DSN 的"无凭据身份"** | 复用现有 `_stable_postgres_identity` 的思路：解析 URL 后取 `host:port/database`，**剔除用户名/密码**。理由与 PG 分支的 docstring 一致 —— 凭据轮换不应导致缓存整体失效 |
   | **必须包含 schema（库名即命名空间）** | MySQL 的 `database` 等价于 PG 的 schema；两个部署连同一实例的不同库**必须**得到不同身份 |
   | **不可解析时的兜底** | 与 PG 分支一致：解析失败则退回原始字符串（仍按部署稳定），**绝不能**退回常量 |
   | **`else` 分支要收紧** | 现状 `else: "memory"` 会把**任何未来新增后端**都静默归并到同一身份。应改为显式枚举 + 未知后端**抛错或派生独立身份**，避免同类问题再次发生 |
   | **前缀里带上格式版本** | `checkpoint_cache_key_prefix` 已有 `ckpt-hist:v{CACHE_FORMAT_VERSION}:`，保持 |

   - **归类**：这是**后续性能优化项的前置修复**（启用 Redis checkpoint cache 之前必须完成），
     **不是 MySQL 迁移的阻塞项** —— 迁移期 `checkpoint_channel_mode = full`，
     `CachedHistorySaver` 根本不挂载，该函数不会被调用。
     但**必须在"第 0 档"变更里一并交付**，不能留到启用时才发现。
2. **缓存失效逻辑要与 schema 变更同步演进**：`threads_meta.metadata_json` 拆出
   `pinned` / `archived`（§20.4）会影响对应缓存的失效条件；
   缓存的键与失效点**必须和 ORM 改动放在同一个变更里**。
   （~~`channel_conversations` 的唯一约束调整（§20.8）~~ ⛔ 第三轮已随渠道删除。）
3. **FK → 应用层级联的代码必须同时承担缓存驱逐**：§20.10 #4 要求把 **2 处** FK
   （第三轮后；原 4 处）改成应用级删除；一旦该路径上的实体被缓存，
   级联删除就必须**连带清缓存**，否则会出现"DB 已删、缓存仍有"的悬挂。
   这是"移除 FK"这项工作的**新增子任务**。
   （原举的"渠道会话映射"例子已随渠道删除；剩下的是 `user_preferences` 与
   `subagent_batch_items` 两条路径。）

### 18.6 Redis Stack 的部署边界：**镜像可用 Stack 7，架构只依赖 OSS 7 core commands**

> **已决策**：**Runtime 只依赖 Redis OSS 7 core commands，
> 不依赖 RedisJSON / RediSearch / TimeSeries / Bloom 等任何 Stack module；
> 可以使用 Redis Stack 7 镜像部署，但架构不能依赖 Stack 特性。**

这两件事必须分开看，否则容易得出错误结论：

| 维度 | 结论 |
| --- | --- |
| **架构依赖** | ✅ **只用 core commands**。禁止把任何 Stack module 作为功能前置条件（不得出现"没有 `FT.SEARCH` 就跑不起来"这类依赖） |
| **部署镜像** | ✅ **允许使用 `redis/redis-stack` 7.x 镜像** —— 只要架构不依赖 module，用哪个镜像只是运维选择（Stack 镜像本身是 OSS 的超集，core commands 完全一致） |
| **两者的关系** | 架构不依赖 module ⇒ **未来换回 OSS 镜像不需要改代码**。这正是"镜像可以放宽、架构必须收紧"的理由 |

**当前 runtime 实际使用的 core commands 白名单**（全仓实测，可作为架构约束的验收清单）：

| 消费者 | 用到的命令 | 命令类别 |
| --- | --- | --- |
| `runtime/checkpoint_cache/redis.py` | `MGET`、`SET … EX`、`SCAN`、`UNLINK`、`PIPELINE(transaction=False)` | String / Keyspace / Pipelining |
| `runtime/stream_bridge/redis.py` | `XADD`（含 `MAXLEN`）、`XRANGE`、`XREVRANGE`、`XREAD`（含 `BLOCK`）、`EXPIRE`、`EXISTS`、`DEL`、`PIPELINE(transaction=True)` | Stream / Keyspace / Transaction |
| `community/aio_sandbox/ownership/redis.py` | `EVAL`/`EVALSHA`（`register_script` × 4）、`GET`（Lua 内部使用 `SET`/`GET`/`DEL` + TTL） | Scripting / String |

**全部是 Redis 7 core commands，无一条属于 Stack module。**
全仓 `JSON.*` / `FT.*` / `TS.*` / `BF.*` **零命中**（§18.1）。

**为什么"逐模块评估"的结论仍然是不用**（即使镜像自带）：

| 模块 | 潜在落点 | 判断 |
| --- | --- | --- |
| **RedisJSON** | 缓存整行文档、`JSON.SET` 局部字段更新（如 `metadata_json` 的 patch） | ❌ **不使用**。缓存整行时 `SET` + JSON 字符串同样够用；一旦使用，架构就**依赖了 module**，换回 OSS 镜像即不可用 —— 收益（少读改写整条记录）远小于"部署基线被锁死"的代价 |
| **RediSearch** | 用 `FT.SEARCH` 支撑线程列表的 metadata 过滤 | ❌ **不使用**。会把 §20.4 刚用"拆 scalar 列"解决的过滤问题**再实现一遍**（第二套查询语义），且 thread 写入面很宽 → 索引一致性成本高；同样会把架构绑定到 module |
| **RedisBloom** | 用 Bloom 做入站去重 | ❌ **不可用（与架构无关，是能力不匹配）**。Bloom 有**假阳性**（会把合法消息误判为重复而丢弃），且**没有逐项 TTL**（无法实现 `INBOUND_DEDUPE_TTL_SECONDS` 语义）。普通 `SET NX EX` 才是对的 |
| **RedisTimeSeries** | 无 | ❌ 与本文档无关 |

**结论**：
1. **部署上不禁止 Stack 镜像** —— 若运维侧已有 `redis-stack` 7.x 基线，可以直接沿用，**不需要为此专门部署 OSS 镜像**。
2. **架构上禁止依赖 Stack 特性** —— 任何新代码**不得**调用 `JSON.*` / `FT.*` / `TS.*` / `BF.*`；
   code review 与 CI 可用上面那条命令白名单做检查（`grep -rnE '\.(json|ft|ts|bf)\.'` 应零命中）。
3. 若将来确实需要 RedisJSON / RediSearch，应当作为**独立提案**评估其对部署基线的影响，
   **而不是夹带在本次数据库迁移里**。

### 18.7 小结（可直接作为结论引用）

> **核心原则（保持不变的判断）**：
> **Redis 只用于可重建缓存或传输状态，不替代 MySQL 的事务一致性与 durable truth，
> 也不会降低 MySQL Schema / CheckpointSaver 本身的迁移复杂度。**

1. **Redis 不能降低 MySQL 的 schema 迁移难度**（§18.2 A）—— 那部分工作一行都不会少。
   **迁移难度的主体（自研 saver 的 DDL/聚合/UPSERT、并发原语替换、schema 硬约束）
   不会因为加了 Redis 而减少一行。**
2. 🔴 **Redis checkpoint cache 不与数据库迁移同时切换**（§18.3 前置约束）。
   🔻 **第五轮加强**：本阶段保持 `checkpoint_channel_mode = full`、**不启用** Redis checkpoint cache、
   **也不为它预置任何 namespace / 失效 / 级联驱逐设计**；
   `delta + Redis checkpoint cache` 作为**迁移完成后的独立变更**评估。
   该项是**后续性能优化项，不是迁移阻塞项**。
3. **唯一"零新增实现"的收益是启用已存在的 `checkpoint_cache`（redis 后端）**，
   它是 saver-agnostic 的，自研 MySQL saver 直接继承 —— 但**启用前必须先补齐
   `checkpoint_cache_db_hash()` 的稳定 database identity derivation**（§18.5 #1），
   否则 MySQL 会退化到共享的 `"memory"` 身份，导致多库共用 Redis 时 **cache namespace collision**。
   🔻 **第五轮**：该修复**同样不在本阶段** —— 缓存不启用 ⇒ 该函数不被调用 ⇒ 不触发。
4. **鉴权读缓存收益最高**（每请求一次 DB 读），但**带安全语义，必须显式失效**（§18.3 第 1 档）。
   🔻 **第五轮**：仍在范围外，且**不预置**。
5. ~~**"把 webhook 去重移出 MySQL"是唯一能让整张表消失的选项**，
   但代价是去重变成有损 —— **列为可选降级路径，不作默认**（§18.4）。~~
   ⛔ **第三轮后本项消失** —— 渠道整体删除，那张表本来就会消失，**不需要用 Redis 绕开它**（§18.4）。
6. 🔴 **Redis 只允许承载"丢失后能从 durable store 重建"的状态。**
   明确**不能**以 volatile Redis 作为唯一 truth source 的：**run ownership、scheduler lease、
   MCP lease、sequence allocation、durable task claim、sandbox ownership**（§18.2 C）。
   判据只有一条：**这份数据丢了之后，系统是否仍能自行重建出正确结果。**
7. ⚠️ **不能假定"Redis 丢数据后 `run_events` 会自动回源"**（§18.1 V4）。
   StreamBridge 的 gap 检测**只覆盖保留窗口被裁掉**，**不覆盖 Redis 重启丢流**；
   SSE 路径**没有** RunEventStore 自动补偿，恢复依赖客户端 `reload_durable_state`。
   🔻 **第五轮定稿**：这是**可靠性项，且不在本次变更范围** ——
   ~~建议在迁移同批次至少补上 gap 检测的空洞~~ **已取消**（§18.3 第 0 档 / O4）。
8. **Redis Stack 边界**：Runtime **只依赖 Redis OSS 7 core commands**，
   不依赖任何 Stack module；**可以用 Stack 7 镜像部署，但架构不能依赖 Stack 特性**（§18.6）。
9. **不要用 volatile Redis 承载真相源** —— 尤其注意现有的**沙箱 ownership store 已属于这一类**，
   需要在部署设计里与缓存实例**显式隔离**（§18.1）。

---

## 19. Recommended Migration Sequence

> 以下为**建议顺序**，按"最高风险优先验证"排列，本任务不执行其中任何一步。
> 核心原则：**先做 Checkpoint 持久化（阶段 2）** —— 如果这一层不可接受，
> 就没有必要继续大规模改 Application Data。因此**不要**把顺序反过来，
> 也不要把 Application Data 的批量改造排在最前面。

> 🔻 **第五轮对本章的四处结构性修改**（范围瘦身，§1.7 是权威口径）：
>
> | # | 修改 | 影响 |
> | --- | --- | --- |
> | 1 | **阶段 1 / 阶段 2 不再包含 LangGraph Store** | 删掉"`ag_store` 列与索引设计""MySQL 最小 store 实现""Store 工厂分支""Store 健康探针"等条目（§9） |
> | 2 | **阶段 2 只实现 async CheckpointSaver** | 不写同步 MySQL Saver；但**同步 DB 驱动（`PyMySQL`）仍在范围内**，因为 `agent_storage.backend: db` 走同步 engine（§8.6） |
> | 3 | **阶段 3 不再有 `0002` / `0003` 两条 revision** | 空库 fresh cutover ⇒ 最终 Schema **一次成型**写在 `0001_mysql_baseline` 里；没有历史数据 ⇒ 无 backfill；应用层级联删除是**代码**不是 revision（§19 阶段 3） |
> | 4 | **阶段 6 增加 Store 删除项** | 与 PG backend 一并删除 `runtime/store/*`（**保留 `_sqlite_utils.py`**） |
>
> 阶段 0 的**位置不变**（渠道删除仍必须最先做），阶段 4 的**内容不变**（并发语义替换不受本次瘦身影响）。
> 阶段 7 的定性**加强**：从"迁移之后再做"升级为"**本次变更范围里根本不设计**"（§18.3）。

### 实施前验证 Gate（V1–V4）

**V1 / V2 / V3 定义为实施前 Gate**：在写对应阶段的代码 / DDL 之前**必须**先有结论。
（V4 是后续可靠性项，不阻塞迁移。）

| Gate | 内容 | 必须完成于 | 不做的后果 |
| --- | --- | --- | --- |
| **V1** | **MySQL 独立链的接线方式**（第四轮重定义；第五轮**收窄**）—— ① 双链隔离：`script_location` + `version_table=ag_alembic_version` + head 缓存按链做键；② `bootstrap_schema` 的 `mysql` 分支（empty / versioned / legacy-refuse 三态）。（~~③ bootstrap 并发保护二选一~~ ✅ **第五轮已定稿：不做 DB 锁，单一 Migrator**，§7.8.5 —— 该项从 Gate 中**移除**） | 🔴 **Schema 实施之前（阶段 1）** | 链配置写错会导致**head 判定串链**、`upgrade` 跑错链或读到对方的 `version_table` |
| **V2** | **`checkpoint_ns` 的真实最大长度** | 阶段 2 的 `ag_checkpoint*` DDL 之前 | `VARCHAR(N)` 定错 → 运行期长度溢出或索引键长超限 |
| **V3** | **`run_events.seq` 在 MySQL 8.0.24 + READ COMMITTED 下的并发分配语义实测** | 阶段 4 的并发语义替换之前 | 序号分配失去串行化 → 1062 → **事件丢失**（§13.7） |
| **V4** | **Redis StreamBridge 重启 / `StreamGap` 后 SSE 是否存在 RunEventStore 自动补偿** | 阶段 4/5 之间（与 StreamBridge 相关的变更前） | 误以为"会自动回源"，实际 SSE **静默丢事件**（§18.1 V4） |

> V3 / V4 的**详细验证方法**分别在 §13.7.3 与 §18.1；
> V1 / V2 的定性见 §20.2 / §20.9，Gate 表见 §20.11。

### 阶段 0（**新增，必须最先做**）：Channel / GitHub Webhook 删除

> **为什么它必须在最前面**：本阶段的产出是"**更小的表集合**"。
> MySQL 独立链的链根 **`0001_mysql_baseline`** 是从当时的 ORM 元数据生成的 ——
> 若渠道表尚未删除，baseline 会把 5 张即将消失的表一起建出来。
> 因此：**渠道删除必须先于 `0001_mysql_baseline` 的编写**。
>
> ✅ **第四轮的一个直接收益**：既然 MySQL 走**独立链**（§7.8），
> 就**不再需要**"先由 baseline 建出渠道表、再写一条 drop migration 删掉"这个绕行 ——
> 渠道表**从一开始就不进入** MySQL baseline。
> 于是原 §7.2 判定的"drop migration + pin 测试语义重定义"这个**唯一真实设计难点也随之消失**。
>
> ⚠️ 本阶段的工作量**不属于 MySQL 迁移**，它是 `feature-inventory` §16 Phase 3 的既定工作。
> 列在此处的目的是**明确前后依赖**，而不是把它算作迁移成本。

**A. 代码 / 配置删除**（依据 `feature-inventory` §12.3 清单）

1. `app/channels/` 整目录（12 个 py + `AGENTS.md`）；
   `gateway/app.py` 的 2 处 lifespan import 与 `include_router(channels.router)`。
2. 3 个路由：`routers/channels.py`、`routers/channel_connections.py`、`routers/github_webhooks.py`；
   `gateway/github/` 全套（`dispatcher` / `registry` / `triggers` / `identity` / `prompts` /
   `app_auth` / `run_policy`）。
3. ⚠️ **先解开双向 import，否则 Gateway 启动失败**（`feature-inventory` D24）：
   `gateway/github/dispatcher.py:31` → `app.channels.message_bus`，
   `app/channels/manager.py:42` → `app.gateway.github.run_policy`。
   **这一步不可跳过**（§16 Phase 3.2 → 3.2b 顺序不可调整）。
4. 先剥离渠道与 Runtime 的交叉点（`feature-inventory` D14）：
   `manager.py` 的 `StreamBridge` 依赖、`gateway/services.py` 的 `channel_user_id` 注入、
   uploads 的 owner-scoped 特例。
5. 配置：`config/channel_connections_config.py`、`config.yaml:channel_connections`；
   `pyproject.toml` 的 4 个渠道 SDK 核心依赖（移除前先确认 import 链已断）。

**B. 数据模型 / Schema 删除**

6. `persistence/channel_connections/`、`persistence/webhook_delivery/` 两个包。
7. 🟡 **`bootstrap.py` 的三个常量：第四轮改为"一个字都不动"**。

| 常量 | 上一版处置（第三轮） | **第四轮处置** |
| --- | --- | --- |
| `_CANONICAL_0019_SCHEMA_FLOOR` | 移除 5 个渠道表键（含全部列名） | ❌ **不改** —— 它与 PG 的 `0019` 规范 schema 一一对应，MySQL 不引用它（§7.8.4） |
| `_BASELINE_TABLE_NAMES` | 移除 4 个渠道表名 | ❌ **不改** —— 它 pin 的是 PG `0001_baseline` 的产物 |
| `_BASELINE_INDEX_NAMES` | 移除 11 个渠道索引名 | ❌ **不改** —— 同上 |

> 🔴 **为什么改成"不动"**：MySQL 走独立链后，这三个常量**完全不为 MySQL 服务**。
> 而它们被 `tests/test_baseline_table_names_constant_matches_0001` 与
> `..._index_names_...` **反向 pin** 在 PG `0001_baseline` 的产物上 ——
> 一旦改动，两个 pin 测试立刻失败，**PG 侧的 bootstrap 也失去保护**。
> 正确做法是让它们保持原样，**随 PostgreSQL backend 在阶段 6 一并删除**（§7.8.4）。
> 这也正好**解掉了上一版判定的"唯一真实设计难点"**。

8. ✅ **不需要 drop migration**（第四轮修正）。
   上一版因为"MySQL 与 PG 共用链、baseline 必然建出渠道表"，
   所以需要"在 `0023` 之后插一条 drop migration"。
   **独立链取消了该需求**：`0001_mysql_baseline` 直接生成裁剪后的 15 张表，
   **从不创建**渠道表。PG 链上的 `0001` / `0009` 保持 immutable、**不动**。

**C. 测试处理（⚠️ 有命名陷阱）**

9. **删除**（约 14 个）：`test_channels.py`、`test_channels_router.py`、
   `test_channel_connections_{config,repository,router}.py`、`test_channel_file_attachments.py`、
   `test_channel_intake_backpressure.py`、`test_channel_user_id_env.py`、
   `test_runtime_channel_config_merge.py`、`test_inbound_dedupe.py`、
   `test_multi_pod_inbound_dedupe.py`、`test_migration_0009_webhook_dedupe.py`、
   `blocking_io/test_channels_ingest.py`、`blocking_io/test_channel_runtime_config_store.py`。
10. 🔴 **绝对不要删这几个** —— 它们名字里有 "channel"，但指的是
    **LangGraph 的 `DeltaChannel`（checkpoint 增量通道）**，与 IM 渠道无关：

    | 文件 | 实际含义 |
    | --- | --- |
    | `test_delta_channel_checkpointers.py` | LangGraph `DeltaChannel` checkpoint 测试 |
    | `test_delta_channel_state.py` | LangGraph `DeltaChannel` state 测试 |
    | `test_bench_checkpoint_channels.py` | checkpoint **channel**（状态通道）基准 |
    | `test_summarize_checkpoint_channels.py` | checkpoint **channel** 摘要 |

    **误删的后果**：直接破坏 checkpoint 迁移的回归基线（阶段 2 依赖它们）。
11. **部分修改**（约 9 个，只删渠道相关断言/夹具，不删文件）：
    `test_persistence_bootstrap.py`、`test_persistence_migrations_env.py`、
    `test_gateway_lifespan_shutdown.py`、`test_projects_router.py`、`test_reload_boundary.py`、
    `test_slash_skills.py`、`test_support_bundle.py`、`test_monocle_tracing.py`、
    `test_trace_entry_points.py`。

12. 🔴 **本阶段退出条件**：`app/channels/` 与 `gateway/github/` 已删除、
    Gateway 可正常启动、`bootstrap.py` 三个常量**保持原样**且两个 pin 测试**仍通过**
    （第四轮修正：**不改**常量，见组 B 第 7 条）、
    上述 4 个 `DeltaChannel` 测试**仍在且通过**。
    **只有满足这些，才可以开始阶段 1（Schema 设计）。**

### 阶段 1：确定 MySQL 独立链 / Schema / Baseline / Driver / Checkpoint 设计

1. 冻结基线：记录 **PG 链** `alembic head = 0023_user_preferences`，确认 `0001`–`0023` **不可变**
   —— 它们从此只作为**旧实现历史**存在，MySQL 侧**不引用**。
2. 把 §7 的规范固化为"MySQL 规范检查清单"，作为后续所有 **MySQL revision** 的验收标准。
3. 🔴 **【Gate V1，必须先于 Schema 实施完成】** **产出 V1 结论**（§20.11）：
   按 **§7.8** 落地 **MySQL 独立链的接线方式** ——
   ① 双链隔离：第二个 `script_location` + `version_table = ag_alembic_version` +
      `_HEAD_REVISION` / `_KNOWN_REVISIONS` **按链做键**；
   ② `bootstrap_schema(..., backend="mysql")` 分支（`empty` / `versioned` /
      `legacy → refuse` 三态，**无** forward-compatible 分支）；
   ③ bootstrap 并发保护**二选一**（`GET_LOCK` 持连接 / 单一 Migrator 不做 DB 锁）。
   —— 定性见 §20.2：`0001_mysql_baseline` 是 **MySQL 链的根**，不是"接在 PG 链后的增量"。
   **验收标准**：见 **§7.8.6** 的 7 条 —— 空实例 → `0001_mysql_baseline` → 顺序 upgrade →
   **单 head** → 不依赖任何 PG migration / PG-specific SQL → 幂等 → 与 PG 链互不影响。
4. **确定驱动**（§20.3；第五轮修正）：`asyncmy` 作为**异步主路径**；
   `PyMySQL` **确认进入范围**（第五轮 §8.6 —— 不再是"待确认"，见下方）：
   - 🔴 **为什么必须保留同步驱动**：`agent_storage.backend: db` ⇒ `SqlAgentStore`
     用**同步** `create_engine(config.database.app_sync_sqlalchemy_url)`
     （`persistence/agents/sql.py:50-52`，模块级 `_engines` 缓存），
     而它的 docstring 明确"the per-run agent build runs in the **graph subprocess**"
     ⇒ 这是**生产路径**，不是调试入口。
   - ⚠️ **不要把它与"同步 CheckpointSaver"混为一谈**：同步 **Saver** 确实没有生产消费者
     （§8.6(2)：`DeerFlowClient` 是唯一调用方，而它在 `app/` / `packages/` 中零生产引用），
     但同步 **engine** 有。二者共用"同步"这个词，结论相反。
   - **验收**：`grep -rn "app_sync_sqlalchemy_url"` 的每个调用点都有明确结论；
     `PyMySQL` 只被 `persistence/agents/sql.py` 这一条同步路径使用。
5. ~~**确定 Channel Schema**（§20.8）：五张表的保留 / 收敛边界，以及 `webhook_deliveries` →
   `delivery_key CHAR(64)` 的主键重设计；同时解决"去重时点 `connection_id` 是否可解析"这一实施约束。~~
   ✅ **第三轮后本项被替换**：渠道整体删除（§1.5），因此本阶段**只需确认阶段 0 的删除已落地**，
   不再需要为渠道设计任何 Schema。原 §20.8 决策作废（见 §20.8 的作废说明）。
6. **确定 Checkpoint Schema 骨架**：`ag_checkpoint` / `ag_checkpoint_blob` / `ag_checkpoint_write` /
   `ag_checkpoint_migration` 的列与索引；`checkpoint_ns` 先按 `VARCHAR(255)` 占位。
   ⛔ **第五轮删除：不再设计 `ag_store`**（§9 —— Store 整体删除，本阶段不建这张表，
   也不做 pgvector / Store 向量迁移的任何讨论）。
7. 🔴 **【Gate V2】** **产出 V2 结论**（§20.11）：采集现有 Lead Agent / SubAgent / 嵌套子图的
   `checkpoint_ns` 长度分布（`max` / `p95` / `p99`），据此定 `VARCHAR(N)` 并加运行时长度断言。
   —— **必须完成于阶段 2 的 DDL 之前**，`VARCHAR(255)` 在验证前只是设计占位。
8. 把 **Checkpointer** 的构造从 `provider.py` / `async_provider.py` 的具体类
   抽象为"按 `config.type` 分派的工厂"，新增 `mysql` 分支（先留空实现）。
   —— **`checkpointer:` 配置节已经存在且优先于 `database:`**（§1.4），
   因此这一步只需扩展 `CheckpointerType` 的 `Literal`，不需要新配置模型。
   ⛔ **不包含 Store**：`make_store` / `_resolve_store_config` 等随 §9 删除，
   **不为它加 `mysql` 分支**（否则等于保留一个没有消费者的抽象层）。
   ✅ **但保留同步 Checkpointer 工厂分支**：不是为生产，而是为了让
   `DeerFlowClient` / 测试**继续可用**（§8.6(2)）—— 同步分支只需支持
   `memory` / `sqlite` 两种类型，**不加 `mysql`**。
9. 把 §13 的并发原语抽象为显式接口：
   `acquire_txn_lock(key)` / `allocate_sequence(table, col, where)` /
   `conditional_upsert(...)`，为每种后端提供实现。**这一步让并发语义可测试**。
10. 在 CI 中加一条**静态检查**：用 MySQL 方言编译全部 ORM 语句，
    断言不出现 `RETURNING`（§20.10 #3、§17.2 #4 的静默陷阱）。

### 阶段 2：MySQL **async** CheckpointSaver + Object Storage Blob（最高风险，最先做）

> 🔻 **第五轮的两处收窄**：
> ① **只实现 async**（`AsyncBaseCheckpointSaver` / `AsyncPostgresSaver` 的对应物）——
>   同步 saver 无生产消费者（§8.6(2)），**不为它写 MySQL 实现**；
>   同步分支继续只支持 `memory` / `sqlite`，`DeerFlowClient` 的本地/调试用法不受影响。
> ② **不含 Store**（§9）—— `ag_store` 不存在，因此"store 实现"与"store 健康探针"
>   两个条目一并删除。**这直接砍掉了阶段 2 约一半的未知量**：
>   剩下的是"一个 saver + 一个 blob 混合策略"，而不是"saver + store + 两者的交互"。

11. 实现 `ag_checkpoint` / `ag_checkpoint_blob` / `ag_checkpoint_write` /
    `ag_checkpoint_migration` 的 **MySQL 方言 async saver**（`AsyncBaseCheckpointSaver` 子类），
    含中文 COMMENT、`ag_` 前缀、`uk_`/`idx_` 索引命名。
    ⚠️ **写入顺序与幂等性必须与 `PostgresSaver` 语义对齐**（`aput` 的
    "先写 checkpoint 行、再写 writes、最后 bump migration"三段式），
    否则 `aresume` / `aget_tuple` 在中断点会读到半成品。
12. ~~实现 `ag_store` 的最小 MySQL store（`get` / `put` / `delete` / `search`）。~~
    ⛔ **第五轮删除**（§9）。**替代方案**：`setup_agent_tool` 的 Agent 定义读取
    走**已有的** `deerflow.persistence.agents.get_agent_store()`（`agents` 表），
    **不新增第二个 Store 抽象**（§9.2 已用证据修正了旧文档的错误说法）。
13. 实现 blob 混合策略（§20.5）：新增配置项 `checkpoint.inline_blob_threshold`，
    `MEDIUMTEXT(base64)` 内联 + Object Storage overflow，并实现
    **"先写对象 → 再写 DB 引用 → commit；commit 失败留 orphan → GC 清理"**的写入顺序。
    —— 本项**不在第五轮的删除范围内**：它是 checkpoint 数据本身的存储策略，
    与 Store 无关。
14. 用现有测试回归：
    `tests/test_delta_channel_checkpointers.py`、`tests/test_run_worker_delta_resume.py`、
    `tests/test_run_worker_rollback.py`、`tests/test_run_duration_checkpoint.py`。
    ⚠️ **保留 `checkpoint_channel_mode = full`**（§18.3）：这 4 个测试覆盖的是
    checkpoint 的 resume / rollback / delta-channel 语义，**与 Redis cache 无关**。
15. **决策点**：若此阶段无法在不违反规范的前提下达标，方案退化为
    `Not currently recommended`。

### 阶段 3：迁移 Application Data 的 ORM / Repository / JSON / DateTime / Index / FK
（第五轮后：**唯一新 revision 是 `0001_mysql_baseline`**，无 backfill、无级联 revision）

16. 🔴 创建 **`0001_mysql_baseline.py`（MySQL 链根，且本阶段唯一的新 revision）**：
    以 MySQL 规范重建全部 **15 张表**（第三轮后；原 20 张。`ag_` 前缀、TEXT 序列化、
    生成列唯一索引、`DATETIME(6)`、无 FK、`DEFAULT` 补齐、中文 COMMENT、`uk_`/`idx_` 命名）。
    —— **它是 MySQL 的链根，不是"接在 PG `0023` 之后的 `0024`"**（第四轮改名；§7.8）。
    —— 该 revision 自带完整建表，因此 **MySQL fresh bootstrap 不需要 `create_all`**。
    ⛔ **不含任何渠道表 / 渠道索引 / 渠道约束**（**从不创建**，因此无需后续 drop migration）。
    🔴 **第五轮强化：它必须一次表达"最终形态"** —— 因为库是空的，
    **所有 `NOT NULL` / `server_default` / 唯一索引 / 生成列都直接写在建表语句里**，
    不经过"先放宽、后收紧"的两步走（下方第 17 条已删除）。
17. ~~创建 **`0002_mysql_backfill_not_null.py`**：执行 §7.3 的 backfill，
    然后把可安全 `NOT NULL` 的列切 `NOT NULL`。~~
    ⛔ **第五轮删除**（本次瘦身第 2 条：不设计历史数据兼容）。
    **理由**：backfill 的前提是"表里已经有历史数据、其中有 NULL"。
    本次是**空库 fresh cutover**（§20.6），**不存在任何待回填的行** ⇒
    "先建可空列、再回填、再切 NOT NULL"三步走**整体失去意义**。
    **替代**：`NOT NULL` 与 `DEFAULT` 直接进 `0001_mysql_baseline`（见第 16 条）。
    ⚠️ 注意这**不等于**"放弃 `NOT NULL` 约束"——约束照加，只是**不需要一条 revision 来加**。
18. ~~创建 **`0003_mysql_cascade_delete.py`**：为 **2 处**被移除的 FK（第三轮后；原 4 处）
    提供**应用层级联删除**，并加入**孤儿巡检**（§20.10 #4）。~~
    **第五轮改判：这不是一条 migration，而是一项应用层代码工作。**
    - 事实：删掉 FK 后，**级联删除的语义必须由应用承担** ——
      2 处（第三轮后；原 4 处。渠道的 2 处随表删除）。
    - 但"应用层级联删除 + 孤儿巡检"**不改变 Schema**，因此它不该占用一个 revision 编号；
      写在 `0003` 里只会让 MySQL 链出现一条**空 migration**（只有注释、无 DDL）。
    - **落位**：并入本阶段第 19 / 20 条的 ORM / Repository 改造，作为其中一项验收点；
      revision 编号**保留给未来真实的 Schema 演进**（MySQL 链的 `0002` 从未来第一个
      真实 DDL 变更开始）。
    - ⚠️ **与 PG 链的编号冲突提醒仍然有效**：两条链的 revision id 必须**全局唯一**，
      MySQL 链未来的文件名应带可辨识前缀（如 `0002_mysql_*`），避免与 PG 链的 `0002_*` 撞 id。
19. 修改 ORM 模型（`__tablename__`、去 `ForeignKey`、`sa.JSON`→`sa.Text`、
    `DateTime(timezone=True)`→`DATETIME(6)` + UTC 写入边界、补 `server_default` 与 `comment=`、
    `threads_meta.metadata_json` 拆 `pinned`/`archived`）。
20. 重写 §3.10 的 6 类违规查询为应用层逻辑，并删除 `persistence/json_compat.py`
    的方言 hack（或整体废弃该模块）。

### 阶段 4：替换并发与 SQL 语义 + 重新验证多实例并发正确性

21. 🔴 **【Gate V3，先实测再动手】** 先完成 **V3**（§13.7.3）：在 MySQL 8.0.24 + **READ COMMITTED** 下
    实测 `SELECT max(seq) … FOR UPDATE` 的真实锁范围与竞态窗口，
    覆盖 **有行 / 空 thread / 同进程 vs 跨进程** 三种组合。
    **只有实测证明能串行化，才允许"删除 advisory-lock 分支、直接走 `else`"**；
    否则改用 **per-thread counter/sentinel row + `SELECT … FOR UPDATE`**
    （与 §20.7 调度器预算锁同一模式），或 **唯一约束 + bounded retry**。
    ⚠️ 注意 `runtime/events/store/db.py:153` 的 `else` 分支**不是通用实现而是 SQLite 语义**
    （SQLite 忽略 `FOR UPDATE`），MySQL 只是静默落入。
22. 替换另外 **1 处**事务级 advisory lock（第三轮后；原 2 处）：
    `scheduled_task_runs/sql.py:317` 调度器全局预算锁 —— **锁表 sentinel row + `SELECT … FOR UPDATE`**，
    或 unique constraint + retry；**不使用 `GET_LOCK`**（§20.7）。
    （~~`channel_connections/sql.py:398` OAuth scope 串行化~~ ⛔ 随渠道删除。）
23. ⚠️ **【Gate V4】** 处理 StreamBridge 的恢复路径（§18.1 V4）：
    至少补上 **gap 检测的空洞**（`earliest_entries` 为空且游标非 `0-0` 时也应判定为 gap），
    否则客户端**收不到"需要恢复"的信号**；完整的 durable replay fallback 可作为后续项。
    —— 本项与 MySQL 迁移**无因果关系**，但迁移后 Redis 被当作"随时可重启的纯缓存"，
    该空洞会**更容易被触发**，因此建议同批次处理。
24. bootstrap 的会话级 advisory lock：✅ **第五轮已定稿**（§7.8.5）——
    改为"**只允许单一 Migrator 执行 Alembic，然后启动多个应用实例**"，
    不为 migration 另造一套数据库分布式锁（**不使用 `GET_LOCK`**，也不使用
    PG 的 `_postgres_lock()` 对应物）。**本项因此从"待决策"降级为"按结论落地"**。
    ⚠️ 若未来确需并发保护，方案是 `GET_LOCK` 持连接（§7.8.5 保留了完整分析），
    但**本阶段不实现**。
25. 替换 **4 处** `RETURNING`（第三轮后；原 5 处。§13.4 的逐条设计）。
26. ~~替换 `dedupe_store` 的条件 upsert（含 `delivery_key` 主键改造与去重时机调整）。~~
    ✅ **第三轮后本项删除** —— 原全表**唯一 High（最高）难度项**，随渠道删除整体消失。
27. 显式设置 `READ COMMITTED`；审计全部 **51 处** `with_for_update` 的索引可用性
    （InnoDB 缺合适索引时锁范围会明显扩大）。
28. 重写 `app/gateway/auth/repositories/sqlite.py` 的错误码判定（`1062` 替代 `23505`）。
29. 重新设计 `mcp_tasks` 的 `FOR SHARE` 归属校验（PG 四档行锁强度模型不成立）。
30. **重新验证多实例并发正确性**：为 **Scheduler / Run ownership / MCP Tasks /
    Subagent Batches 四个职责域**（第三轮后；原五个，Inbound dedupe 已随渠道删除）
    分别写 MySQL 并发测试，并**首次定义 MySQL 上的跨实例对账语义**
    （`scheduled_task_runs/sql.py:758` 原文 "Multi-instance reconciliation is Postgres-only."）。
31. 🔴 **本阶段退出条件（并发语义验收）**：V3 有明确结论并已按结论落地、
    **四个**职责域的 MySQL 并发测试通过、`run_events` 在高并发下**不出现 1062 导致的事件丢失**。

### 阶段 5：基础设施 + Application DB 与 Checkpointer 分阶段切流

32. 新增 MySQL 驱动依赖 + `mysql` extra（按阶段 1 的驱动结论）：
    **`asyncmy`（异步主路径）+ `PyMySQL`（同步路径）两个都要**。
    ⛔ 原"**不预置 `PyMySQL`**"的说法**第五轮撤销** —— 它是错的：
    `agent_storage.backend: db` 的 `SqlAgentStore` 用同步 engine，是生产路径（§8.6(3)）。
    ⚠️ 但**不要**因此新增"同步 CheckpointSaver 的 MySQL 实现"（§8.6(2) 已排除）。
33. `persistence/engine.py`：新增 MySQL engine kwargs（`pool_pre_ping`、
    `pool_recycle`、`max_execution_time`），移除 `_auto_create_postgres_db` 或改为
    `CREATE DATABASE IF NOT EXISTS`。
34. `app/gateway/health.py`：新增 `mysql` 探针（`_probe_checkpointer_backend` 的
    `Literal` 也要扩展）。
    ⛔ **第五轮删除：不需要 Store 探针**（§9 —— Store 不存在）。
    ⚠️ 注意 `health.py:176` 引用 `runtime/store/_sqlite_utils.py` 的
    `resolve_sqlite_conn_str` ⇒ **删 Store 时该文件必须保留**（§9.3 耦合 1），
    否则健康检查直接 ImportError。
35. `deploy/helm/`：新增 MySQL StatefulSet 或 external DSN 支持；
    更新 `values.yaml` / `NOTES.txt` / postgres-secret 模板。
36. 新增 MySQL 专属集成测试（对应现有 8 个 PG 测试文件），
    并把 🔻 **113 个**命中 `postgres` 的文件分批参数化（第五轮重算；原记 111）。
37. **分阶段切流**：利用 `database:` 与 `checkpointer:` 可分别配置的能力，
    **先切一个职责域、再切另一个，不要一次性切换**；每阶段都保留 PG 作为 rollback path。
38. ⚠️ **切流期必须保持 `checkpoint_channel_mode = full`**（§18.3 前置约束）：
    不为利用 Redis 缓存而同时改变 checkpoint 存储语义。若确需 `delta`，
    必须等 **MySQL Saver 稳定后**作为**独立变更**评估。

### 阶段 6：稳定期结束后彻底删除 PostgreSQL

39. 删除 PG Driver、PG Saver/Store、PG Schema helper、PG 专属 SQL
    与 `persistence/json_compat.py` 的方言分支。
    🔴 **同时删除 §7.8.4 的 legacy 清单**（第四轮新增的**显式动作**）：
    `_CANONICAL_0019_SCHEMA_FLOOR`、`_BASELINE_TABLE_NAMES`、`_BASELINE_INDEX_NAMES`、
    `_BASELINE_REVISION`、`_FORWARD_COMPATIBLE_REVISION`、`_validate_forward_schema()`、
    `_run_baseline_create_all_sync()`、`_postgres_lock()` / `_PG_LOCK_KEY`，
    以及 `bootstrap.py` 的 `legacy` 与 `forward-compatible` 两个分支
    与两个反向 pin 测试；**并移除 PG 链的 migrations 目录**（含 `0001`–`0023`）。
    —— 注意：`_run_create_all_sync()` **保留**（SQLite / 开发路径仍在用）。
40. 删除 Helm PostgreSQL 配置、compose 的 PG 服务、`postgres` extra 与依赖。
41. 删除 / 改写 PG 专属测试（含 `_PG_LOCK_KEY` 相关用例），
    把参数化测试收敛为 **MySQL 单后端**（§20.6）。
42. 文档：更新 `backend/AGENTS.md`、`config.example.yaml`、`README*`，
    并明确"最终只维护 MySQL"这一结论。

### 阶段 7（**迁移之后，独立变更**）：Redis 侧的可靠性 / 性能优化项

> ⚠️ **本阶段不阻塞迁移，也不与迁移同时进行。** 列入此处是为了明确"这些是后续项"，
> 避免实施时误把 §18 的缓存建议当成迁移的一部分。

43. **Redis checkpoint cache 单独评估**（§18.3 第 0 档）：
    - 前置：补齐 `checkpoint_cache_db_hash()` 的**稳定 database identity derivation**
      （§18.5 #1 —— 不能退化到共享的 `"memory"` 身份）；
    - 然后才评估 `checkpoint_channel_mode: delta` + `database.checkpoint_cache.type: redis`；
    - 作为**独立变更**交付，便于失败归因。
44. **鉴权读缓存 + 渠道会话映射缓存**（§18.3 第 1 档）：
    鉴权缓存**带安全语义**，必须在所有递增 `token_version` 的写路径显式删键。
45. **StreamBridge 的 durable replay fallback**（§18.1 V4 的完整形态）：
    若阶段 4 只补了 gap 检测的空洞，此处补"从 `RunEventStore` 按 `seq` 补齐并推送"的完整路径
    （需要把 SSE 的 `event_id` 改为可回源到 DB 的标识，属**接口级改动**）。
46. **Redis 部署边界核查**：确认 runtime **只依赖 OSS 7 core commands**，
    无 `JSON.*` / `FT.*` / `TS.*` / `BF.*` 调用（§18.6 的命令白名单）；
    若使用 `redis-stack` 7.x 镜像，验证换回 OSS 镜像**无需改代码**。

---

## 20. Design Decisions（原 Open Questions，已收敛）

> **本节性质已变更。** 第二版此处是 12 项 Open Questions；经项目负责人逐项确认后，
> 除 **4 项必须实测（V1–V4，其中 V1/V2/V3 为实施前 Gate）** 外，
> 其余**全部已形成设计决策**，不再作为待决项。
> 本轮另外新增 **§20.12 的 4 项 Redis 相关决策（R1–R4）**。
> 「相对原建议的变化」一列标出哪些决策**收紧了**原来的默认值 ——
> 这些是最容易按旧默认实施而走偏的地方，实施时应以本节的「最终决策」为准。

### 20.1 已确认的决策（12 项问题 / 13 个决策点，Q1 拆为两个）

| 项目 | 最终决策 | 相对原「建议默认」的变化 |
| --- | --- | --- |
| **Q1 Migration 载体** | **继续使用 Alembic Python revision，不改成裸 SQL** | 与建议一致 |
| **Q1 MySQL Fresh Baseline** | 🔴 **MySQL 使用独立 migration chain，`0001_mysql_baseline` 为链根**；**不要求、也不允许**重放 PostgreSQL `0001`–`0023` | ⚠️ **第四轮定性改变**（原为"接在 PG 链之后的 `0024`"，已取消；§20.2 / §7.8） |
| **Q2 表名** | **统一机械增加 `ag_` 前缀，不额外做语义重命名** | 与建议一致 |
| **Q3 Driver** | **异步主路径使用 `asyncmy`；只有确实保留同步 DB 路径时才增加 `PyMySQL`** | ⚠️ **收紧**（§20.3） |
| **Q4 `scopes`** | **`TEXT` 保存 JSON 数组** | 一致，并明确禁止第二套字符串协议（§20.4） |
| **Q5 Checkpoint Blob** | **`MEDIUMTEXT` 内联 + Object Storage overflow 的混合方案** | ⚠️ **细化**：阈值改为配置项（§20.5） |
| **Q6 `MEDIUMTEXT`** | **允许，但只用于确有大文本需求的 checkpoint 等字段，不泛化使用 `LONGTEXT`** | 与建议一致 |
| **Q7 PostgreSQL / MySQL 双后端** | **仅迁移期暂时共存；MySQL 上线后 PostgreSQL backend 直接废弃** —— **不迁移任何 PG 历史数据、不 dual-read / dual-write、不 backfill、不支持 PG 原地升级** | ⚠️ **第四轮收紧**：定性明确为 **fresh-cutover / backend replacement**（§20.6） |
| **Q8 `GET_LOCK`** | **不作为业务事务锁方案使用**（⚠️ 但 **bootstrap 串行化**是例外，理由见 §7.8.5） | ⚠️ **第四轮补充例外辨析**（§20.7） |
| **Q9 Alembic Version Table** | **使用 `ag_alembic_version`** —— 第四轮起它同时承担 **MySQL 独立链的 `version_table`**（与 PG 链的 `alembic_version` 隔离） | 与建议一致，且**用途扩展**（§7.8.2） |
| **Q10 MySQL 版本** | **严格锁定 MySQL 8.0.24** | 与建议一致 |
| ~~**Q11 Channel / Webhook**~~ | ~~**Channel 核心保留，但重新整理 5 张表职责；Webhook 去重表重构**~~ | ⛔ **已作废（第三轮）**：改为**整体删除**（§1.5 / §20.8） |
| **Q12 `checkpoint_ns`** | **暂不拍死长度，先实测真实 namespace 长度再定 `VARCHAR(N)`** | ⚠️ **改为待实测项**（§20.9） |

### 20.2 Q1：Fresh Baseline 的定性 —— **MySQL 独立链的链根**（第四轮改写）

保留 `Alembic + Python revision`，因为 `bootstrap.py`、`ScriptDirectory`、revision 检测、
`stamp`/`upgrade`、`_helpers.py` 的幂等 helper 与 `_env_filters` 的 autogenerate 保护
全部围绕 Alembic 建设。改裸 SQL 等于把"换数据库"扩大成"重写迁移框架"，没有必要。

**定性（第四轮）**：

> `0001_mysql_baseline` 是 **MySQL 自己那条 Alembic 链的根（chain root）**，
> **而不是**"接在 PG `0001`–`0023` 之后的 `0024` 增量"。
> MySQL 链与 PG 链**各有自己的 `script_location` 与 `version_table`**，互不引用。

三点必须说清：

1. **不再有"空库伪装成 PG 历史状态"的风险** —— 该风险在第四轮**彻底消失**：
   MySQL 的链里**没有** `0001`–`0023`，因此不存在"伪装"这件事。
   上一版为了规避它才提出 `0024`，而 `0024` 本身又引入了 **multiple-heads** 问题 ——
   **独立链一次解决两者**。
2. **不需要 drop migration**：`0001_mysql_baseline` **直接**生成裁剪后的 15 张表，
   渠道表**从不进入** MySQL（§1.5 / §7.8.3）。
3. **`0018_oauth_identity_pg_partial` 在 MySQL 侧不存在**：
   它读 `pg_index` / `to_regclass`（§7.7），但 MySQL 链从不执行它。

**这一处定性的代价（必须写清）**：`0001_mysql_baseline` 必须**一次写全**裁剪后的 Schema
（15 张表 / 224 列 + 47 个索引 + 3 个生成列唯一索引 + 中文 COMMENT）。
它是一份**较大但一次性**的 revision，且是后续所有 MySQL revision 的基线 ——
**写错就要改链根**，因此必须完整通过 **§7.8.6** 的 7 条验收。

### 20.3 Q3：驱动最小化 —— **第五轮定稿：两个驱动都要，但各有明确归属**

`mysql+asyncmy://` 是**异步主路径**（FastAPI / SQLAlchemy async）。
`PyMySQL` **确认进入范围**，但它**只服务一条同步路径**。

> **原则仍然成立**："生产主路径需要什么就引入什么，不为潜在兼容提前增加依赖。"
> 第五轮只是把"什么算生产主路径"这个事实查清楚了 ——
> 结论是**确实存在一条同步生产路径**，因此 `PyMySQL` 不再是"潜在兼容"。

**第五轮的事实更正（推翻本节上一版的"待确认"结论）**

| 问题 | 上一版（待确认） | **第五轮实测结论** |
| --- | --- | --- |
| `app_sync_sqlalchemy_url` 在 MySQL 方案中是否仍被使用？ | "只有确认后才加 `PyMySQL`" | ✅ **仍被使用，且是生产路径** |
| 谁在用？ | — | `SqlAgentStore`（`persistence/agents/sql.py:50-52`）用**同步** `create_engine`；由 `make_agent_store`（`persistence/agents/__init__.py:37-57`）在 `db_backend ∈ {sqlite, postgres}` 时构造 |
| 触发条件是什么？ | — | `agent_storage.backend: db`（`config.yaml:230`，**当前就是 `db`**）⇒ `agents_api.enabled: true`（`config.yaml:199`） |
| 为什么说它是生产路径？ | — | `persistence/agents/__init__.py:61+` 的 docstring 明确："the per-run agent build runs in the **graph subprocess**" —— 每个 run 构建 agent 时都会走这里 |
| 它会被 §8.6 的"删同步 Saver"顺带删掉吗？ | — | ❌ **不会**。同步 **Saver** 与同步 **engine** 是两件事（§8.6(3)） |

**因此本节的结论改为**：

```
asyncmy  → 异步主路径（Gateway / Agent Runtime / async Checkpointer / RunEventStore）
PyMySQL  → 同步路径，唯一用途：SqlAgentStore（graph subprocess 里的 agent 定义读取）
```

**不要做的事**（第五轮新增的两条禁令）：

- ⛔ **不要因为引入了 `PyMySQL` 就顺手实现"同步 MySQL CheckpointSaver"。**
  `PyMySQL` 的存在**不是**同步 Saver 的理由 —— 同步 Saver 的消费者是
  `DeerFlowClient`（§8.6(2)），它在 `app/` / `packages/` 中**零生产引用**。
  同步 Checkpointer 分支继续只支持 `memory` / `sqlite`。
- ⛔ **不要为"未来可能有别的同步消费者"提前把同步 Saver 也做上。**
  这与本次瘦身的原则一致：**先删除不需要的能力，再迁移必须保留的能力。**

**验收**：`grep -rn "app_sync_sqlalchemy_url"` 的**每个**调用点都要有明确结论；
若最终只剩 `persistence/agents/` 一处，则 `PyMySQL` 的引入范围就是这一处。

### 20.4 Q4：JSON → TEXT，热点键拆列

规范禁用 MySQL `JSON` 类型，因此现有 25 个 `sa.JSON` 列统一改为
`TEXT` + 应用侧 JSON 序列化。项目本身已有这种模式
（`engine.py:25-27`、`events/store/db.py` 的 `_content_to_db`）。

`personal_access_tokens.scopes` 继续保存 `["threads:read","runs:create"]` 形态，
**不要**变成 `threads:read,runs:create` —— 避免人为发明第二套字符串协议。

真正需要按 JSON key 查询的热点字段，**不再用 `JSON_EXTRACT()` 复刻 PostgreSQL 方言逻辑**，
而是把热点字段拆成普通 scalar 列。例如 `threads_meta.metadata_json` 额外拆出
`pinned`、`archived`，原始 metadata `TEXT` 仍然保留。

### 20.5 Q5 / Q6：Checkpoint Blob 的混合存储与写入顺序

小 payload 内联 MySQL，大 payload 放 Object Storage。理由是规范不允许 `BLOB`，
而普通 `TEXT` 上限仅约 64 KB；同时 HEAD 已具备 S3/MinIO 对象存储端口，
**不需要再新建一套存储基础设施**。

```
小 blob  → MEDIUMTEXT(base64) 内联
大 blob  → Object Storage；DB 只存 blob_ref / blob_size / blob_sha256 + 序列化类型信息
```

**阈值不得写死。** 文档原先出现的 `256 KB` **不是永久固定值**，应改为配置项：

```
checkpoint.inline_blob_threshold
```

再通过代表性的 Lead Agent / SubAgent / 长会话 workload 采集 checkpoint blob 大小分布，
据此决定默认阈值。

**对象存储写入顺序必须固定为：**

1. 先写 Object Storage；
2. Object 写成功后，再写 MySQL `blob_ref`；
3. MySQL transaction commit；
4. 若 MySQL commit 失败，允许留下 orphan object；
5. 后续通过 GC 清理 orphan。

> **不要先提交 DB 引用再上传对象** —— 否则会产生
> "Checkpoint 已存在但 Blob 不存在"的**不可恢复状态**。

类型边界（对应 Q6）：普通文档字段默认 `TEXT`；只有**已证明**可能超过 `TEXT` 上限的字段
（如 checkpoint serialized payload）允许 `MEDIUMTEXT`；**暂时没有必要使用 `LONGTEXT`**。
这样既满足实际 payload，又避免所有 JSON/Text 字段无脑升级成大字段。

### 20.6 Q7：双后端只存在于迁移期

`database:` 与 `checkpointer:` 已可分别配置（§1.4），这是降低迁移风险的重要能力；
但**不意味着最终产品应长期支持 `postgres | mysql` 两套生产后端**。否则今后
锁语义、Upsert、Migration、Health Check、Checkpoint Saver、Integration Test
都必须长期维护两套。
（~~Store~~ ⛔ 第五轮已从范围中删除，§9 —— 因此**不需要**为它维护两套。）

| 阶段 | PostgreSQL 实现的定位 |
| --- | --- |
| **迁移阶段** | 保留，但**只是"尚未切走的后端"，不是数据回滚路径**（见下方 🔴） |
| **迁移完成并稳定后** | **删除** PG Driver、Saver、Schema helper、PG 专属 SQL、测试与部署配置 |

最终只维护 MySQL。

**🔴 第四轮补充：本次是 fresh-cutover / backend replacement，不是数据迁移**

| 问题 | 结论 |
| --- | --- |
| 需要迁移 PostgreSQL 历史数据吗？ | ❌ **不需要** |
| MySQL 上线后 PostgreSQL backend 怎么处理？ | **直接废弃**（不保留只读、不保留影子库） |
| 需要支持 PostgreSQL 原地升级到 MySQL 吗？ | ❌ **不支持**（没有 in-place upgrade 路径） |
| 需要 dual-read / dual-write / backfill 吗？ | ❌ **都不需要** |
| 那"迁移期双后端"到底指什么？ | **只指"配置可切换"**：两条职责链（`database:` / `checkpointer:`）可**分别**指向不同后端，用于**分阶段切流**与**切换前的回退**；**不是**长期数据同步，**也不是数据层回滚**（见下） |
| 能"回滚到 PostgreSQL"吗？ | 🔻 **第五轮定稿：不能，也不设计这条路径。** 因为**没有数据迁移**，切到 MySQL 后 PG 侧**从未写入过新数据** ⇒ "切回 PG"等于**丢掉切换后产生的所有数据**，那不是回滚，是数据丢失。**"可回退"的准确含义只是**：在**尚未切走**的职责域上，把配置改回 PG（该域的数据还没进 MySQL） |

> 🔻 **第五轮补充：这一条如何"减负"**
>
> 既然没有数据层回滚，就不需要为它准备任何东西：
> **不需要**回滚 migration、**不需要**双向 schema 兼容、**不需要**"MySQL 写入的数据能倒回 PG"的设计、
> **不需要**为"切回去之后 MySQL 侧多出来的数据怎么办"定义策略。
> 切流是**单向**的：一个职责域切到 MySQL 并验证通过后，它就**不再回到 PG**；
> 出问题时的处置是**向前修**，而不是向后切。
> 这直接消掉了"回滚路径"这一整类复杂度（也是 §1.7 B 组第 8–11 项被删除的原因）。

> 由此产生一个直接后果：**MySQL 的 migration 链不需要为"从 PG 导入数据"做任何准备** ——
> 没有 backfill migration、没有数据校验步骤、没有双写对账。
> 这也正是 §7.8.3 能把 MySQL bootstrap 简化到"**只剩 `upgrade(head)` 一条路径**"的原因。

### 20.7 Q8：锁方案

`GET_LOCK()` 是 **connection-scoped** 而非 transaction-scoped，
不适合作为现有 PostgreSQL 事务级 advisory lock 的直接替代
（连接归还池时锁不释放）。业务并发控制优先使用：

- **sentinel row + `SELECT … FOR UPDATE`**，或
- **unique constraint + retry**。

Schema Migration 更简单：**只允许单一 Migrator 执行 Alembic，然后再启动多个应用实例** ——
不要为了 migration bootstrap 再实现一套数据库分布式锁。

> 🔻 **第四轮提出的例外辨析，第五轮已关闭（结论：不做）**：
> §7.8.5 曾提出 MySQL 的 **bootstrap 串行化**可以考虑 `GET_LOCK` ——
> 这**不与本节矛盾**，因为**临界区边界不同**：
> 业务事务锁的临界区是一个**短事务**、连接会**立刻归还池**（⇒ 锁泄漏）；
> 而 bootstrap 的临界区是**整个 bootstrap 过程、连接被持有到结束**（⇒ 锁存活期正好匹配）。
>
> **✅ 第五轮定稿：本阶段不做任何 bootstrap DB 锁。**
> 理由是**前提变了**：本次所有 MySQL 实例都是**空库**（§1.7、§20.6），
> migration 只执行一次、且可以由**单一 Migrator** 在启动多实例之前完成
> （§7.8.5）。既然不需要"多实例同时 bootstrap"这个场景，
> 就不必为它引入 `GET_LOCK` 及其"必须同一连接持有到结束"的实现约束。
> 该分析**保留在 §7.8.5 备查**，供未来真出现并发 bootstrap 需求时使用。
> —— 该项因此**从 V1 的关闭项中移除**（§20.11）。

⚠️ 现有 **51 处 `FOR UPDATE`** 仍必须逐个检查索引是否充分：
InnoDB 在缺少合适索引时锁范围会明显扩大，这是 RR + gap lock 下最容易出问题的地方（§13.2）。

### 20.8 ~~Q11：Channel 五张表的职责与长期去向~~ ⛔ **已作废（第三轮）**

> ⛔ **本节决策已被第三轮架构决策取代，不再实施。**
>
> 本节原先的结论是"Channel **当前不删除**，只把 5 张表分成
> '保留 / 短期保留 / 旁路' 三类，并把 `webhook_deliveries` 改成
> `delivery_key CHAR(64)` 主键"。
>
> **第三轮决策改为：Channel 与 GitHub Webhook 整体删除**
> （沿用 `feature-inventory` 的既有决策 Q2 / Q19 / C7 / C14 / E8，见 §1.5），
> 因此：
> - 这 5 张表**全部删除**，不存在"保留 / 收敛 / 长期去向"问题；
> - `delivery_key CHAR(64)` 主键重设计**不再实施**（§15.6 同步作废）；
> - "去重时点 `connection_id` 是否可解析"这个实施约束**不再需要解决**；
> - `channel_credentials → Credential Broker`、`channel_oauth_states → OAuth Provisioning`
>   这两条长期演进路径**本阶段不再规划**（未来重新设计 Custom Channel 时另行讨论）。
>
> **本节保留仅为历史记录。** 下面的分析对"未来基于统一事件入口重新设计
> Custom Channel"仍有参考价值（尤其是"Credentials / OAuth 属旁路基础设施"这一判断）。
>
> 下文为第三轮之前的原文，未做修改。

Channel 当前**不删除**（后续需要支持自定义 Channel），但**没有必要把现有 5 张表
全部定义成永久 Channel Core**。

| 表 | 当前职责 | 最终建议 |
| --- | --- | --- |
| `channel_connections` | 真实渠道连接 | **保留** —— 未来 Custom Channel 的核心连接实例模型 |
| `channel_conversations` | 外部会话 → 内部 Thread 映射 | **保留** —— 与具体 Slack / Feishu / 企业微信 Adapter 无关的通用映射 |
| `webhook_deliveries` | 多实例入站消息去重 | **保留去重能力，但重构 Schema**（见下） |
| `channel_credentials` | 加密 access / refresh token | **短期保留；长期并入统一 Credential Broker** —— 未来更合理的是 `channel_connections.credential_ref → Credential Broker`，而不是 Channel 自己维护完整 Token 加密 / 刷新 / 过期体系 |
| `channel_oauth_states` | OAuth / PKCE 临时握手态 | **非 Channel Runtime 核心** —— 属 OAuth Provisioning；未来若 Credential Broker / Integration 层统一负责 OAuth，可迁出 Channel 模块 |

→ 未来 Channel Core 收敛为 **Connection + Conversation Mapping + Inbound Event Deduplication**；
Credentials / OAuth 属于旁路基础设施。

**Webhook 去重表重构**：当前四字段主键在 utf8mb4 下约 **8448 字节**，
超过 InnoDB 3072 字节上限，MySQL **无法按现状建表**（§4.7）。
改为通用 Event Deduplication：

```sql
delivery_key CHAR(64) PRIMARY KEY   -- = SHA256(provider + connection_id + external_event_id)
```

原始 channel / conversation / message 信息继续作为普通字段保存，用于审计与排障。

> ⚠️ **实施约束（必须在设计阶段解决，不能留到编码时才发现）**
> 现有去重键是 `(channel_name, workspace_id, chat_id, message_id)`
> （`app/channels/manager.py:1634-1667`、`dedupe_store.py:25-27`），
> **用的是 `channel_name` 而不是 `connection_id`**；且去重发生在 `manager.py:1670`，
> **早于**连接解析（`_apply_effective_owner` / `_get_bound_identity_rejection`）。
> 因此改用 `connection_id` 参与 `delivery_key` 之前，必须先确认该时点 `connection_id` 已可解析，
> 或把去重时机后移到连接解析之后。此外必须保留现有的 **fail-closed** 语义 ——
> 拿不到 workspace / 连接标识时**跳过去重**，而不是把不同 workspace 的消息误判为重复。

### 20.9 Q12：`checkpoint_ns` 长度 —— 仍待实测

`checkpoint_ns` 属 LangGraph 内部 namespace，并参与 checkpoint 复合主键。
嵌套 graph / subgraph 会导致其长度增长，而当前**没有足够证据**证明
150、255 或其他长度就是正式上限。

先针对现有 **Lead Agent / SubAgent / 嵌套 SubGraph / 不同 namespace 深度**
做一次真实长度统计，拿到 `max` / `p95` / `p99` 之后，再决定 `VARCHAR(N)`，
并加**运行时长度断言**。

`VARCHAR(255)` 仅作为**设计占位**，在验证前**不是最终 Schema 契约**。
若 LangGraph 本身没有稳定最大长度约束、真实场景又可能突破合理索引长度，
再考虑"完整 namespace + namespace hash"的设计，而不是一开始就增加复杂度。

### 20.10 已明确、不再作为待决项

| # | 事项 | 结论 |
| --- | --- | --- |
| 1 | **时间字段** | 统一改为 **UTC `DATETIME(6)`**。当前 **48 个** `DateTime(timezone=True)`（第三轮后；原 61）在 MySQL 方言下会变成无时区、默认无亚秒精度的 `DATETIME`，因此必须明确**应用边界统一使用 UTC**，并显式采用微秒精度 |
| 2 | **Partial Unique Index** | 用 **Generated Column + Unique Index** 替代。现有 **3 个** PG partial unique index（第三轮后；原 4 个，`uq_channel_connection_active_identity` 已随渠道删除）不能直接迁移，但可利用 MySQL 唯一索引允许多个 `NULL` 的特性实现**精确等价**语义（§15.3） |
| 3 | **`RETURNING`** | **必须全部清除**（第三轮后 **4 处**；原 5 处）。尤其要增加一条 **MySQL 方言 SQL 编译检查** —— SQLAlchemy 2.0.49 可能把 `UPDATE … RETURNING` 编译出来却**不提前报错**，直到真正执行才出现 MySQL 1064。**这个问题必须进入 CI 防回归**（§17.2 #4） |
| 4 | **外键移除后的级联语义** | **2 个**数据库 FK（第三轮后；原 4 个，其中 Channel 的 2 个随表删除）—— 剩 `user_preferences` 1 个、`subagent_batch_items` 1 个。禁止后**不能只删约束**，还必须补齐**应用级删除**以及必要的**孤儿数据检查**（§4.5）。🔻 **第五轮改判**：这是**应用层代码工作，不是一条 migration** —— 它不改变 Schema，因此不占用 revision 编号（§19 阶段 3 第 18 条） |
| 5 | **Vector / RAG** | **不迁入 MySQL**。pgvector 实际未启用，知识 / RAG 已在外部体系，不应为了"数据库统一"新增 MySQL Vector 设计。🔻 **第五轮补充**：本项**已随 LangGraph Store 的整体删除而变得无对象** —— 项目里唯一会用到向量检索的入口是 LangGraph Store，而 Store 不迁移（§9）。因此"不迁入 MySQL"从一条**设计取舍**降级为一条**无需讨论的事实**（§9.5） |
| 6 | **LangGraph MySQL Checkpoint Saver** | **自研 Adapter，不直接采用第三方实现**。第三方使用 `JSON` / `LONGBLOB` / `BINARY(16)`，与规范直接冲突，内部 SQL 又大量依赖 JSON 函数；fork 后改造量已接近自研（§16.2） |
| 7 | **LangGraph MySQL Store** | 🔻 **第五轮新增：不存在这个待决项 —— 因为 Store 不迁移，整体删除。** 原文档中"自研 MySQL Store""第三方 MySQL Store 评估""`ag_store` Schema""Store 健康 / 配置 / provider 迁移要求"**全部删除**（§9）。替代路径：Agent 定义读取复用已有的 `agents` 仓储（§9.2） |

### 20.11 仍需验证的 4 项（V1–V4，其余均已决策）

**V1 / V2 / V3 定义为实施前 Gate**：在写对应阶段的代码 / DDL 之前**必须**先有结论。
V3 / V4 是第二轮**新增**的验证项（此前被错误地当作"已可判定"）；
**V1 在第四轮被重定义** —— 原为"`0024` 与 revision graph / multiple heads 的关系"，
现为"**MySQL 独立链的接线方式**"（§7.8）。

> 🔻 **第五轮：项数不变（仍是 4 项），但 V1 变窄、V4 的定性被加强。**
>
> | 项 | 第五轮变化 |
> | --- | --- |
> | **V1** | **收窄**：原 ③ "bootstrap 并发保护二选一" **移出 Gate** —— 已定稿为"不做 DB 锁"（§7.8.5）。剩 ① 双链接线 + ② `mysql` bootstrap 分支，仍是硬 Gate |
> | **V2** | **不变**（`checkpoint_ns` 长度实测） |
> | **V3** | **不变**（`run_events.seq` 并发语义实测）—— 本次瘦身**没有**触及并发原语，因此这项的必要性完全保留 |
> | **V4** | **定性加强**：从"后续可靠性项"进一步明确为**完全不在本次变更范围**（§18.3 第五轮定稿）。它**不阻塞**迁移，也**不随**迁移一起做 |
>
> ⚠️ **本次瘦身没有减少任何 Gate 的数量，也没有降低任何一项的难度。**
> 它减少的是"Gate 之外要写多少代码"。这一点在 §21.2 有明确说明。

| # | 待验证项 | 验证方法 | 阻塞什么 | 归类 |
| --- | --- | --- | --- | --- |
| **V1** | **MySQL 独立链的接线方式**（第四轮重定义；原为"`0024` 与 revision graph 的关系"。🔻 **第五轮收窄**：去掉原 ③ bootstrap 并发保护二选一 —— 已定稿为"不做 DB 锁、单一 Migrator"，§7.8.5） | 按 **§7.8** 落地：① 第二个 `script_location` + `version_table = ag_alembic_version` + `_HEAD_REVISION` / `_KNOWN_REVISIONS` **按链做键**；② `bootstrap_schema(..., backend="mysql")` 的 `empty` / `versioned` / `legacy → refuse` 三态（**无** forward-compatible 分支）。**验收：§7.8.6 的 7 条**（空实例 → `0001_mysql_baseline` → 顺序 upgrade → **单 head** → 不依赖任何 PG migration / PG-specific SQL → 幂等 → 与 PG 链互不影响） | 🔴 **Schema 实施之前（阶段 1）**，否则链配置写错 ⇒ **head 判定串链**、`upgrade` 跑错链或读到对方的 `version_table` | **实施阻塞（Gate）** |
| **V2** | **`checkpoint_ns` 的真实最大长度** | 对现有 Lead Agent / SubAgent / 嵌套子图采集 namespace 长度分布（`max` / `p95` / `p99`），据此定 `VARCHAR(N)` 并加运行时断言 | `ag_checkpoint*` 的 `VARCHAR(N)` 定长（阶段 2 的 DDL） | **实施阻塞（Gate）** |
| **V3** | **`run_events.seq` 在 MySQL 8.0.24 + READ COMMITTED 下的并发分配语义** | 双连接 / 双事务并发 `put_batch`，用 `performance_schema.data_locks` 或 `SHOW ENGINE INNODB STATUS` 观察真实锁对象；覆盖 **有行 / 空 thread / 同进程 vs 跨进程** 三种组合；确认是否出现 **1062**（§13.7.3） | 阶段 4 的 advisory-lock 替换方式（能否直接走 `else` 分支），以及**并发正确性验收**（阶段 4 退出条件） | **实施阻塞（Gate）** |
| **V4** | **Redis StreamBridge 重启 / `StreamGap` 后 SSE 是否存在 RunEventStore 自动补偿** | 代码层面**已确认"没有自动补偿"**（§18.1 V4 的证据链）；待实测的是"丢流后订阅端是否真的静默继续"（`redis.py:291` 的 gap 条件在 `earliest_entries` 为空时被跳过） | 不阻塞 MySQL 迁移；阻塞"能否声称 SSE 具备可靠恢复能力" | **后续可靠性项**（非迁移阻塞） |

**V3 的结论分支**（实施时必须二选一，不能悬空）：

```
V3 实测 → 能串行化 ──→ 删除 advisory-lock 分支，保留 stmt.with_for_update()
        └ 不能串行化 ─→ (a) per-thread counter/sentinel row + SELECT … FOR UPDATE   【推荐】
                        (b) 唯一约束 + bounded retry（uq_events_thread_seq 已存在，但当前无重试）
```

### 20.12 Redis 相关决策（本轮新增，4 项）

| 项目 | 最终决策 | 理由 / 影响 |
| --- | --- | --- |
| **R1 checkpoint cache 的切换时机** | 🔴 **不与数据库迁移同时切换，且第五轮进一步加强为"本次变更范围内根本不设计"**。本阶段保持 **`checkpoint_channel_mode = full`**、`database.checkpoint_cache.type` 保持默认（不生效）；**不为未来的 Redis cache 预置任何 namespace / invalidation / cascading eviction 结构**；`delta + Redis checkpoint cache` 作为**迁移完成后的独立性能优化项**评估 | 避免"换数据库"与"改 checkpoint 存储语义"叠在同一变更窗口 → **失败归因不可能**（§18.3）。第五轮补充理由：**现在设计这些结构的形状等于凭猜测给当前实现加约束**，猜错就会变成 MySQL 侧的历史包袱 |
| **R2 `checkpoint_cache_db_hash()` 的 MySQL 身份** | **必须补齐稳定的 database identity derivation**，**不能**让 MySQL 退化到共享的 `"memory"` 身份 | 否则两个 MySQL 部署共用 Redis 时 **cache namespace collision** → 互相命中对方的 checkpoint 历史（§18.5 #1）。**启用缓存的前置修复，非迁移阻塞项** |
| **R3 Redis 的职责边界** | **只允许承载"丢失后能从 durable store 重建"的状态**。**run ownership / scheduler lease / MCP lease / sequence allocation / durable task claim / sandbox ownership** 均**不得**以 volatile Redis 作为唯一 truth source | 判据：**这份数据丢了之后，系统是否仍能自行重建出正确结果**（§18.2 C） |
| **R4 Redis Stack 的依赖边界** | **Runtime 只依赖 Redis OSS 7 core commands**，不依赖 RedisJSON / RediSearch / TimeSeries / Bloom；**可以使用 Redis Stack 7 镜像部署，但架构不能依赖 Stack 特性** | 架构不依赖 module ⇒ 换回 OSS 镜像**无需改代码**；镜像选择放宽为运维自由（§18.6 附 core-commands 白名单） |

### 20.13 Sandbox ownership 的 Redis 持久化 —— **本阶段不决策**（第三轮新增）

**决策：不在本次 MySQL 迁移中决定 sandbox ownership 的持久化方案。**

| 项 | 内容 |
| --- | --- |
| **本阶段只确认一条约束** | 🔴 **sandbox ownership 属于 lease / correctness state，不得放入"重启即空"的 volatile Redis。** |
| **为什么不在本阶段决策** | 该问题的正确答案取决于**执行环境（sandbox runtime）的整体改造方向**，而 MySQL 迁移不改变沙箱的所有权模型。在迁移文档里给出方案会**把两个独立的变更窗口耦合在一起**（与 R1 同一个道理）。 |
| **留给谁** | **后续执行环境改造阶段**（sandbox runtime refactor）单独决策。 |
| **现状事实（不因本决策而改变）** | 当前实现 `community/aio_sandbox/ownership/redis.py` **已经**把容器所有权租约放在 Redis 上（`own:` / `del:` 前缀 + TTL + 4 个 Lua 脚本，issue #4206），**是真相源而非缓存**（§18.1 第 3 行）。也就是说，它的问题不是"要不要搬过去"，而是"**已经在那里了，且与'重启即空'前提冲突**"。 |
| **与 MySQL 迁移的关系** | **无直接因果关系**。MySQL 迁移**不需要**它落地，它也**不阻塞**迁移。因此归入 §21.3 的**非阻塞项**。 |
| **本阶段唯一需要保证的事** | 在部署拓扑上**不要把 sandbox ownership 的 Redis 实例与"随时可重启的纯缓存"实例混用**（至少要在配置上可分离），否则未来无论选哪种持久化方案都会被缓存实例的重启策略牵连。 |

> 换句话说：本阶段**只锁定约束，不锁定方案**。这样既不会让一个未决问题阻塞迁移，
> 也不会让"用 Redis 当缓存"这个新前提悄悄地把沙箱所有权推进 volatile 存储。

---

## 21. Final Feasibility Assessment

### 21.1 逐项回答（**任务书** §20 的 13 个问题）

**1. 当前项目是否能够完全移除 PostgreSQL？**
**能，但不是"零成本移除"。** 应用层（**15 张表**、ORM、仓储；第三轮删渠道前为 20 张）
可以在不改变领域模型的前提下迁移。
Checkpoint 层目前**完全依赖** `langgraph-checkpoint-postgres` 3.1.1 的
`PostgresSaver` / `AsyncPostgresSaver`，
其 SQL 与 DDL 硬编码 PostgreSQL 语义（`JSONB`、`BYTEA`、`jsonb_each_text`、
`array_agg`、`ANY(%s)`、`CREATE INDEX CONCURRENTLY`、`%s` 占位符）。
必须用自研实现替换，这不是"移除依赖"，而是"重写一个持久化组件"。
🔻 **第五轮补充**：同包的 `PostgresStore` / `AsyncPostgresStore` **不需要替换 —— 它们整体删除**
（§9）。因此"重写一个持久化组件"的**范围进一步收窄为"只重写 CheckpointSaver"**，
Store 那一半的未知量消失。
另外 **2 处**事务级 advisory lock / **4 处** `RETURNING` / **3 处** partial index
（第三轮后；原 3 / 5 / 4）也必须重写。
**且"完全移除"有明确终点**：按 §20.6 的决策，PostgreSQL 实现只在**尚未切走**期间保留，
稳定后**删除** —— 不长期维护双后端，**也不设计数据层回滚**（§20.6 第五轮补充）。

**2. 是否能够使用 MySQL 8.0.24？**
**能。** 8.0.24 具备全部必需的 SQL 能力：`FOR UPDATE`、`FOR SHARE`（编译为 `LOCK IN SHARE MODE`）、
`SKIP LOCKED`、CTE、窗口函数、函数索引（8.0.13+）、生成列、
`ON DUPLICATE KEY UPDATE`、行别名 `AS new`（8.0.19+）、`LAST_INSERT_ID(expr)`、
`TEXT` 的表达式默认值（8.0.13+）。
⚠️ **但 `SELECT max(…) FOR UPDATE` 只是"可编译/可执行"，其串行化语义尚未验证**
（原判"实测可用 → 可直接替代 advisory lock"已撤销，见 §13.7 / V3）——
"具备该语法"与"具备该语义"是两件事。
不具备的只有 `RETURNING`、partial index、事务级 advisory lock、
`CREATE INDEX CONCURRENTLY`、`idle_in_transaction_session_timeout` —— 这五项都有替代路径。

**3. 是否能够严格满足本任务规定的 MySQL Schema 规范？**
- **Application Data：能。** **20 个** JSON 列中 17 个转 `TEXT`、3 个过滤键拆 scalar 列
  （第三轮后；原 25 列中 22 个）；
  **2 处** FK 移除；**3 处** partial index 用生成列唯一索引**精确等价**替代；
  **15 张**表加 `ag_` 前缀；补齐中文 COMMENT 与 `DEFAULT`；**47 个**索引重命名为 `uk_`/`idx_`。
  ⛔ ~~`webhook_deliveries` 主键改为短哈希列~~ —— **表已随渠道删除，本项不再需要**。
- **Checkpoint / Store：不能直接复用任何现成实现。**
  官方无 MySQL saver；第三方 `langgraph-checkpoint-mysql` 3.0.0 使用
  `JSON` + `LONGBLOB` + `BINARY(16)`，**违反规范** → 结论是**自研 Adapter**（§20.10 #6）。
  规范张力已按 §20.5 解决：普通字段用 `TEXT`，**仅** checkpoint serialized payload
  允许 `MEDIUMTEXT`，超出 `checkpoint.inline_blob_threshold` 的走对象存储
  （HEAD 已有端口），且**禁止泛化 `LONGTEXT`**。

**4. Application Data 迁移难度？**
**Moderate。** 主要是机械改造（类型映射、命名、注释、`DEFAULT`、时间精度），
**第三轮后只剩 3 个非机械点**（原 5 个）：
(a) `threads_meta` / `runs` 的 JSON 过滤需拆列（§20.4）；
(b) **2 处**级联删除需从 FK 移到应用层，并补孤儿巡检（§20.10 #4）；
(d) 时间精度与时区需要系统性审计（**48 个列**）→ 统一为 **UTC `DATETIME(6)`**（§20.10 #1）。
⛔ ~~(c) `webhook_deliveries` 主键必须重设计（唯一"建表就失败"的表）~~
—— 表已删除，**本域不再有任何"建表就失败"的问题**。
⛔ ~~(e) 去重键要从 `channel_name` 改为 `connection_id`，需调整去重时机~~
—— 随渠道删除消失。
**因此本域难度是"降低"，而不是"持平"** —— 这是第三轮唯一一处
**难度档位实质下降**的职责域（Hard → 仍是 Moderate，但非机械点由 5 个减为 3 个）。

**5. LangGraph Checkpoint 迁移难度？**
**Hard。** 需要自研 saver（含 DDL、`SELECT` 聚合、UPSERT、DeltaChannel 两阶段查询、
`setup()` 迁移链），并决定 blob 落地策略。这是整个方案的技术风险中心。

**6. LangGraph Store / Memory 迁移难度？**
🔻 **第五轮改判：不存在这个迁移项 —— Store 整体删除，难度归零。**
（上一版的回答是"Moderate（自研）→ Hard（若使用第三方）"，基于一个**已被证据推翻的前提**。）

- ⛔ **不实现 MySQL Store、不创建 `ag_store`、不做第三方 Store 评估、
  不做 Store 健康 / 配置 / provider 迁移、不做 pgvector / Store 向量迁移讨论**（§9）。
- ✅ **为什么可以整体删除**（§9.2 的证据）：
  Store 的**唯一真实消费者**是 `MemoryThreadMetaStore`
  （`persistence/thread_meta/memory.py:25-27`），而它**只在 `database.backend=memory` 时**被选中；
  在 DB 模式下 `make_thread_store(sf, store)` 直接返回 `ThreadMetaRepository(sf)`，
  **`store` 参数被忽略**。`deps.py:435` 的 `make_store(config)` 虽然**无条件构造**了 Store，
  但全仓 **5 处 `store=` 挂载点全部只挂载、不读写**。
- 🔴 **上一版的一处事实错误已更正**：旧文档称 Store 承载 `agents` 命名空间、
  被 `setup_agent_tool` 读取 —— **这是错的**。
  `setup_agent_tool.py:9,61,73` 用的是 `deerflow.persistence.agents.get_agent_store()`，
  即**自建的 `AgentStore`（`agents` 表）**，与 LangGraph `BaseStore` **无关**（§9.2）。
- ✅ **Agent 定义持久化走已有仓储**：复用 `agents` 表 / `SqlAgentStore`，
  **不新增第二个 Store 抽象**。
- **注意**：DeerFlow 的"长期记忆"（`deermem`）与 PostgreSQL **完全无关**
  （本地文件 + SQLite FTS5），不在迁移范围内 —— 这一条**未变**。

**7. Queue / Scheduler 迁移难度？**
**Hard。** **四套**子系统（第三轮后；原五套，**Inbound dedupe 已随渠道删除**）——
Scheduler / MCP Tasks / Subagent Batches / Run ownership ——
分别依赖 advisory lock、`RETURNING`、partial unique index、
`SKIP LOCKED`、PG 专有行锁强度模型，且 **51 处 `FOR UPDATE`** 需要在 RR 隔离级别下
重新核验索引可用性与锁范围。默认隔离级别从 RC 变 RR 会引入 gap lock 与新的死锁模式。
必须为每个原语写 MySQL 专属并发测试。

**额外的一点**：`scheduled_task_runs/sql.py:758` 明写
"Multi-instance reconciliation is Postgres-only." ——
**跨实例对账的正确性语义目前只在 PostgreSQL 上被定义过**。
这意味着本职责域的迁移不只是"替换原语"，还要**首次定义** MySQL 上的对账语义，
并把 PG 专属分支逐条穷举出来（§10.2 #18）。

**本轮修正的一点**：本域中 `run_events.seq` 的分配**原被判为 Easy**，现已上调为 **Moderate**
（§13.7、§10.2 #15）。原因：`runtime/events/store/db.py:153` 的 `else` 分支
**不是通用实现，而是 SQLite 语义**（SQLite 忽略 `FOR UPDATE`，正确性来自库级单写者锁），
MySQL 只是**静默落进同一分支**；"`SELECT max(…) FOR UPDATE` 在 MySQL 能编译/能执行"
**不构成**"具备 PG advisory lock 串行化语义"的证据。必须完成 **V3 实测**（MySQL 8.0.24 +
**READ COMMITTED** 下的并发分配语义）才能决定实现方式。
另外，`run_events` 的 `put` / `put_batch` **没有任何 seq 冲突重试**，
因此一次分配竞态 = **整个 batch 事务失败（事件丢失）**，而不是"重试一次就好"。

**8. Knowledge / Vector 应如何处理？**
**保持现状，不进 MySQL。** 当前**未使用 pgvector**（`PostgresStore` 未配置 embeddings，
向量迁移从未执行）；RAG 走外部 RAGFlow HTTP；本地检索走 SQLite FTS5。
应当固化为"Knowledge Service 独立于 Agent Runtime 数据库"的边界。

**9. 哪些第三方组件虽然支持 MySQL，但违反当前 MySQL 规范？**
- **`langgraph-checkpoint-mysql` 3.0.0**（第三方，非官方，作者 Theodore Ni）：
  `JSON` 列、`LONGBLOB` 列、`BINARY(16)`、无 `ag_` 前缀、无中文 COMMENT、
  索引命名不符、大量 JSON 函数。
- **附带说明 1**：`langgraph-checkpoint-postgres` 3.1.1 虽然当前在用，但它是 PG 专用，
  不属于"支持 MySQL 但不合规"这一类。
- **附带说明 2**：`SQLAlchemy 2.0.49` 本身**不是**违规组件，
  但它在 MySQL 方言下**不报错地**编译出 `UPDATE … RETURNING`，
  属于"工具会掩盖问题"的一类风险（§10.2 #17），必须用静态检查兜住。

**10. 是否存在必须继续保留 PostgreSQL 的能力？**
**严格来说没有"能力"必须保留，但有三个"实现便利"会失去：**
- **事务级 advisory lock**：MySQL 无等价物，`GET_LOCK` 是连接级。
  按 §20.7 的决策，**`GET_LOCK` 不作为业务事务锁方案使用**，
  改用锁表 sentinel row + `SELECT … FOR UPDATE`（或 unique constraint + retry）；
  Schema Migration 则退化为"**单一 Migrator 串行执行 Alembic，再启动多实例**"，
  不需要为 bootstrap 另造数据库分布式锁。
- **Partial / Filtered Index**：MySQL 无原生支持，必须用生成列或函数索引模拟
  （语义可精确等价，但 DDL 可读性与写放大开销不同）。
- **PG 的四档行锁强度模型**：`mcp_tasks` 的 `FOR SHARE` 归属校验**依赖它**，
  在 MySQL 上需要重新设计。
除此之外，PG 的其它用法（`RETURNING`、`ON CONFLICT`、`JSONB`、`BYTEA`、
`search_path`、`CREATE INDEX CONCURRENTLY`）都有替代路径。

**11. 哪些数据应该迁移到 Object Storage，而不是 MySQL？**
1. **Checkpoint 大 blob**（`checkpoint_blobs.blob` / `checkpoint_writes.blob`）——
   最高优先，因为 `messages` 是累积 reducer，blob 随会话长度增长，必然超出 `TEXT` 上限。
2. `mcp_tasks.result_artifact`（`max_result_bytes: 65536`）。
3. `subagent_batch_items.result` / `result_preview`（`max_result_chars: 100000`）。
4. 大 tool 结果（**HEAD 已走对象存储**）。
5. Uploads 与 thread outputs（**HEAD 已走对象存储**）。
> **重要**：前 3 项需要新增对象键命名空间（`keys.py` 目前只有
> `outputs` / `.tool-results` / `uploads` / `skills/custom` 四类）。
>
> **阈值不写死**：由配置项 `checkpoint.inline_blob_threshold` 控制（§20.5），
> 默认值需用真实 Lead Agent / SubAgent / 长会话 workload 的 blob 尺寸分布标定。
> **写入顺序固定为"先写 Object Storage → 再写 DB 引用 → commit"**；
> commit 失败允许留下 orphan object，由后续 GC 清理 ——
> 反过来（先提交 DB 引用）会产生"Checkpoint 在、Blob 不在"的**不可恢复状态**。

**12. 最大的三个迁移风险是什么？**
1. **Checkpoint 持久化层没有合规实现**（§17.1 风险 1）—— 唯一可能推翻整个方案的点。
   ⚠️ **本轮的整改使它更难被缓解**：Redis checkpoint cache 已决策**不与迁移同时切换**
   （§20.12 R1），因此**不能再靠"启用缓存"来抵消 checkpoint 读路径成本** ——
   该风险在迁移期**保持"极高"，不降档**。
2. **并发语义变化**（§17.1 风险 2）—— advisory lock / `RETURNING` /
   partial index / RC→RR，属于"能跑但可能静默错误"；
   （~~条件 upsert~~ ⛔ 第三轮已随渠道删除，本风险少了一个典型场景）
   且 **`RETURNING` 在 SQLAlchemy 下编译期不报错**，是最容易漏的陷阱。
   ⚠️ **本轮新增同类项**：`run_events.seq` 的分配（**V3**）——
   `SELECT max(…) FOR UPDATE` 同样是"编译/执行通过但语义未验证"，
   且因为**没有 seq 冲突重试**，竞态后果是**事件丢失**而非自动纠正（§13.7）。
3. **Schema 层硬约束**（§17.1 风险 3）—— 其中 ~~**`webhook_deliveries` 主键 8448 字节
   超 InnoDB 3072 上限**~~ ⛔ **第三轮后该确定性阻塞已消失**（表已删除，§4.7）；
   本风险降为"设计核对项"：3072 字节键长、`TEXT` 不能作主键、`DATETIME` 截断亚秒、
   `timezone=True` 静默丢弃时区、`TEXT` 64KB 与 blob 体积冲突。
   ⚠️ **注意**：三大风险里**只有第 3 项被第三轮实质削弱**；
   第 1 项（checkpoint 无合规实现）与第 2 项（并发语义）**丝毫未变**。

**13. 推荐迁移顺序是什么？**
见 §19，已按"**最高风险优先验证**"重排为
**1 个前置阶段（阶段 0：Channel 删除）+ 6 个迁移阶段 + 1 个迁移后阶段**：

0. **（第三轮新增，必须最先做）删除 Channel / GitHub Webhook** ——
   `app/channels/`、`gateway/github/`、5 张渠道表。
   🔴 **这是迁移的前置条件，不是迁移本身的一部分**；
   ⚠️ **第四轮修正**：`bootstrap.py` 三个常量**不要动**（§19 阶段 0 组 B）；
   ⚠️ 注意**不要误删** 4 个 `DeltaChannel` 测试（§19 阶段 0 组 C）；
1. **确定 MySQL 独立链的接线 / Schema / Baseline / Driver / Checkpoint Schema**
   —— 🔴 **Gate V1 必须先完成**（否则 head 判定串链、`upgrade` 跑错链，§7.8）；
   🔻 **第五轮**：本阶段**不再设计 `ag_store`**；驱动结论为 `asyncmy` + `PyMySQL`（§20.3）；
2. **先实现并验证 MySQL **async** CheckpointSaver + Object Storage Blob**
   —— 🔴 **Gate V2 必须先完成**（否则 `VARCHAR(N)` 定错）；
   🔻 **第五轮**：**不含 Store**、**不含同步 Saver**（§8.6 / §9）；
3. 迁移 Application Data 的 ORM / Repository / JSON / DateTime / Index / FK，
   并写 **`0001_mysql_baseline`**（MySQL 链根，含裁剪后的 15 张表）。
   🔻 **第五轮**：这是本阶段**唯一的新 revision** —— 没有 backfill revision，
   也没有"级联删除"revision（§19 阶段 3 第 17 / 18 条）；
4. 替换 Advisory Lock、`RETURNING`、Partial Index 等并发与 SQL 语义，
   并**重新验证多实例 Scheduler / Run / MCP / Subagent Batches 四个职责域的并发正确性**
   —— 🔴 **Gate V3 必须先完成**（`run_events.seq` 的并发分配语义实测）；
5. Application DB 与 Checkpointer **分阶段切流**（利用两者可分别配置的能力，不要一次切完）
   —— **切流期保持 `checkpoint_channel_mode = full`**（§20.12 R1）；
   🔻 **第五轮**：切流是**单向**的，**不设计数据层回滚**（§20.6）；
6. 稳定期结束后**彻底删除 PostgreSQL** Driver / Saver / Schema helper /
   部署配置与专属测试，**并删除 §7.8.4 的 legacy bootstrap 逻辑与 PG migration 链**；
   🔻 **第五轮新增**：**同时删除 `runtime/store/*`**（Store 不迁移，§9）——
   ⚠️ 但**必须保留 `_sqlite_utils.py`**（§9.3 耦合 1）；
7. **（迁移之后，独立变更）** Redis 侧的可靠性 / 性能优化项
   （checkpoint cache 单独评估、鉴权缓存、durable replay fallback、Stack 依赖核查）。
   🔻 **第五轮加强**：这些**不在本次变更范围**，本阶段**不为它们做任何预置设计**（§18.3）。

核心原则：**先做 Checkpoint（阶段 2，最高风险）**，
再做 Application Data（阶段 3），最后做并发语义替换与切流（阶段 4/5）。
不要把顺序反过来 —— 如果阶段 2 失败，前面所有 Application Data 的工作都白做。
**前置例外**：**阶段 0（Channel 删除）必须在阶段 1 之前完成** ——
它决定了 Schema 的**范围**（15 张表而非 20 张），也决定了 `0001_mysql_baseline` 的内容；
但它本身**不是 MySQL 迁移的一部分**，而是一项独立的能力下线。
**另一个前置例外**：**Gate V1 / V2 / V3 必须在对应阶段开工前关闭**，不能边做边验。
🔻 **第五轮补充一句**：阶段 0 与阶段 6 各包含一项"**删除**"动作（渠道 / Store），
它们都不是"迁移"，而是"**先删掉不需要的，再迁移剩下的**"这一原则的具体落点。

### 21.2 整体结论

> ## `Feasible with significant changes`

**决策收敛状态**：§20 的 12 项 Open Questions **已全部收敛为设计决策**（§20.1–§20.10；
其中 **§20.8 已作废**，由第三轮的"渠道整体删除"取代），
并新增 4 项 Redis 相关决策（§20.12 R1–R4）
与 **1 项显式"不决策"**（§20.13 sandbox ownership 持久化 —— 只锁约束、不锁方案），
因此本结论不再依赖任何"未定的配置选择"。剩余待验证的是 **4 项（V1–V4，§20.11）**：

| Gate | 性质 | 是否改变本档结论 |
| --- | --- | --- |
| **V1** MySQL 独立链的接线方式（第四轮重定义；🔻第五轮**收窄**） | **实施前 Gate**（必须先于 Schema 实施） | ❌ 不改变 —— 影响阶段 1 产出物。⚠️ 第四轮**验证面缩小**（multiple-heads 与"重放 PG 历史"风险随独立链消失）；🔻 第五轮再缩（bootstrap 并发保护已定稿为"不做 DB 锁"） |
| **V2** `checkpoint_ns` 的真实最大长度 | **实施前 Gate**（必须先于阶段 2 DDL） | ❌ 不改变 —— 影响 `VARCHAR(N)` 取值 |
| **V3** `run_events.seq` 在 MySQL + RC 下的并发分配语义 | **实施前 Gate**（阶段 4） | ❌ 不改变档位，但**抬高了"并发语义替换"这一代价的下限**（原判 Easy → Moderate）。🔻 第五轮**完全不受影响**（瘦身未触及并发原语） |
| **V4** Redis StreamBridge 的 SSE 恢复路径 | 🔻 **第五轮改为"完全不在本次变更范围"**（原为"后续可靠性项"） | ❌ 不改变 —— 与 MySQL 迁移无因果关系，且本阶段不为它做任何改动（§18.3） |

> **第二轮的两处自我修正不改变档位，但改变了"工作量与风险的分布"**：
> ① `run_events.seq` 由 **Easy → Moderate**，且从"删掉分支即可"变成"**必须先实测**"；
> ② Redis 相关的收益**全部后移**到迁移之后（§20.12 R1），
> 即**迁移期的实际工作量比第一轮呈现的更多，而不是更少**。

**第四轮（fresh-cutover + 独立链）的净效果：**

| 变化 | 对结论的影响 |
| --- | --- |
| **取消 `0024_mysql_baseline`，改走 MySQL 独立链** | ⬇️ **消除 multiple-heads 风险**与"重放不适用的 PG 历史 revision"风险 |
| **`0001_mysql_baseline` 直接体现裁剪后的 Schema** | ⬇️ **取消 drop migration** 需求；渠道表"**从不创建**" |
| **bootstrap 三常量改判为 legacy（一个字都不动）** | ⬇️ 上一版判定的"**唯一真实设计难点**"**整项消失** |
| **MySQL bootstrap 退化为 `upgrade(head)` 单路径** | ⬇️ 不需要 `create_all` / schema-floor 校验 / forward-compatible 分支 / legacy 分支 |
| **`0018` 的 PG 系统目录依赖** | ✅ **由构造消除** —— 原"第一号规范偏差"作废，**不再需要用户确认** |
| **确认无历史数据迁移 / 无 dual-read-write / 无 backfill** | ⬇️ **没有 backfill migration、没有数据校验、没有双写对账** |
| **CheckpointSaver 自研 / 并发语义替换 / Schema 规范改造** | ➡️ **丝毫未变** —— 三大代价的结构完全不变 |

**第五轮（迁移范围瘦身）的净效果：**

| 变化 | 对结论的影响 |
| --- | --- |
| **LangGraph Store 整体删除（不迁移）** | ⬇️ 砍掉"自研 MySQL Store + `ag_store` Schema + Store 健康/配置/provider 迁移"**整块未知量**；阶段 2 从"saver + store + 二者交互"变成"**一个 saver**" |
| **删除全部历史数据库兼容设计** | ⬇️ 原 `0002_mysql_backfill_not_null` / `0003_mysql_cascade_delete` 两条 revision **消失**；MySQL 链在 cutover 时**只有 `0001_mysql_baseline` 一条** |
| **Channel 相关 MySQL 工作删除 + 权威重算** | ⬇️ 表 20→**15 张应用表**、列 274→**224**、索引 58→**47**、FK 4→**2**、partial unique 4→**3**、`RETURNING` 5→**4**、`ON CONFLICT` 2→**1** |
| **不做 Redis checkpoint cache** | ➡️ **零工作量变化**（本来就在范围外），但**取消了"预置 namespace / 失效 / 级联驱逐设计"的潜在新增量** |
| **只实现 async CheckpointSaver** | ⬇️ 少写一个 MySQL saver 实现；⚠️ 但 **`PyMySQL` 仍在范围内**（同步 **engine** 有生产消费者，§8.6） |
| **不做无关索引优化** | ⬇️ 明确"只保证必要索引 + 并发正确性"，把冗余索引清理 / 查询调优 / 索引合并**全部推到 MySQL 上线后**（§15.7） |
| **bootstrap 不做 DB 锁** | ⬇️ 少一个并发原语实现；V1 的 ③ 关闭 |
| **CheckpointSaver 自研 / 并发语义替换 / Schema 规范改造** | ➡️ **丝毫未变** —— 三大代价的结构完全不变 |
| **`with_for_update` 51 处 / V2 / V3** | ➡️ **数量与难度均未变**（瘦身只删"不用的能力"，不删"在用的并发原语"） |

> **一句话（五轮合并看）**：第一轮建立基线，第二轮撤销一处过于乐观的难度评级，
> 第三轮移走一整块范围，**第四轮把迁移定性为"backend 替换"并收敛了迁移链的组织方式**，
> **第五轮按"先删不需要的、再迁必须的"原则做了一次范围瘦身**。
> 五轮叠加的结果是：
>
> **档位仍是 `Feasible with significant changes`，且不能下调。**
> **缩减的是"要迁移多少东西"，不是"迁移有多难"** ——
> 唯一可能推翻整个方案的 **CheckpointSaver 自研**，以及**并发语义替换**这两项，**丝毫未变**。
>
> 🔴 **第五轮最容易产生的误读**：看到"表从 26 张减到 20 张、Store 整块删除"，
> 就以为难度下降了。**没有。** 被删掉的都是**本来就不该进这次变更**的东西；
> 剩下的部分（saver 自研 + 并发语义 + Schema 规范）**一件没少、一件没易**。
> 本次瘦身降低的是**失败面**（少改一处就少一处出错机会），不是**单点的技术难度**。

**判定依据（代码证据）**

| 支撑 `Feasible` 的证据 | 支撑 `significant changes` 的证据 |
| --- | --- |
| MySQL 8.0.24 具备全部必需 SQL 原语（除 `RETURNING`/partial index/advisory lock，且这三项均有替代路径） | `langgraph-checkpoint-postgres` **3.1.1** 的 DDL 使用 `JSONB` + `BYTEA` + `CREATE INDEX CONCURRENTLY`（`.venv` 逐行实测） |
| 应用侧已有"JSON 文本 + 类型标记"的序列化模式（`engine.py:25-27`、`events/store/db.py` 的 `_content_to_db`） | LangGraph 官方**无** MySQL checkpointer/store（`ls` + `grep -rli mysql` 双证据，零命中） |
| **3 处** partial unique index（第三轮后；原 4）均可用生成列唯一索引**精确等价**替代（MySQL 唯一索引允许多个 `NULL`） | 第三方 `langgraph-checkpoint-mysql` 3.0.0 用 `JSON` + `LONGBLOB` + `BINARY(16)`（PyPI 实测） |
| 配置模型**已支持**两条职责链用不同后端（独立的 `checkpointer:` 节优先于 `database:`） | **4 处** `RETURNING`（第三轮后；原 5）无 MySQL 等价物，且 SQLAlchemy **不报错地**编译出非法 SQL（实测） |
| Vector / RAG **已经**在数据库之外（RAGFlow HTTP + SQLite FTS5），pgvector 未启用 | **2 处**事务级 advisory lock（第三轮后；原 3）无事务级等价物 |
| 🔻 **Store 使用面已被查清（第五轮，§9.2）**：唯一真实消费者是 `MemoryThreadMetaStore`（仅 `database.backend=memory` 时）；DB 模式下 Store **被构造但从不读写** ⇒ **整体删除可行**（⚠️ 旧文档"仅 `setup_agent_tool` 的 `get`"的说法**已被证据推翻**） | 默认隔离级别 RC → RR，引入 gap lock 与新的死锁模式；51 处 `FOR UPDATE` 需重新核验 |
| **Object Storage 端口已在 HEAD 落地**（`object_storage/port.py`，S3/MinIO），方案 C 成本大幅下降 | ~~🔴 **`webhook_deliveries` 主键 8448 字节 > InnoDB 3072 上限**，按现状建表就失败~~ ✅ **第三轮后已消除**（表删除；剩余最大键 1788 字节） |
| `runtime/events/store/db.py:146-153` 的 advisory-lock 分支**需先完成 V3 实测**才能决定能否删除（`SELECT max(…) FOR UPDATE` 在 MySQL 只是"可编译/可执行"，**不等于**具备 PG advisory lock 的串行化语义；且该 `else` 分支原为 SQLite 语义，§13.7） | **48 个** `DateTime(timezone=True)` 列（第三轮后；原 61）会静默截断亚秒并丢弃时区 |
| 项目已有幂等 migration helper（`_helpers.py`）；**且 `migrations/AGENTS.md:128` 已有"独立 Alembic 链 + 独立 `version_table`"的现成先例**（第四轮） | 🔻 **20 张表**（第五轮权威重算：15 张应用表 + 5 张框架表；第三轮后为 21 张）**+ 113 个**命中 `postgres` 的 `.py` 文件（第五轮重算；原文档记 111）需要系统性改造 |
| ✅ **MySQL 独立链使 fresh bootstrap 退化为 `upgrade(head)` 单路径**（第四轮），且**无历史数据迁移 ⇒ 无 backfill / 无对账 / 无双写** | checkpoint blob 体积与 `TEXT` 64KB 上限冲突，必须放宽类型或引入对象存储 |
| 🔻 **被删能力的规模已被量化（第五轮，§1.7 E）**：`mcp_tasks` 45 + `subagent_batches` 17 + `subagent_batch_items` 23 = **85 列（占 38%）**，均为 `enabled: false` 的 B 类能力 —— 是否进一步删除**待用户决策** | 🔻 **7 处 `("sqlite","postgres")` 能力闸门**在 MySQL 下会 `SystemExit` / `RuntimeError`（第五轮新发现，§17.2 #18）—— 必须逐处扩展，遗漏一处就**启动失败** |

**不推荐的情况**：如果组织无法接受
(a) 自研并长期维护一个 MySQL checkpoint saver，或
(b) 引入对象存储来承载 checkpoint 大 payload，
那么结论应当退回到 **`Not currently recommended`** ——
因为剩下唯一的选择是使用违反本项目 Schema 规范的第三方包
（`langgraph-checkpoint-mysql` 3.0.0），
那与本任务的核心目标（严格满足 MySQL 规范）直接冲突。

### 21.3 整改后的交付结论（**第五轮**）

#### （1）修改后的总体结论

> ## `Feasible with significant changes`（**档位不变**）

**结论档位不变，但"代价的构成"发生了变化。** 四轮整改的**方向并不一致**：
第二轮**增加**了工作量（撤销过于乐观的难度评级），第三轮**缩减**了范围，
第四轮**降低了迁移链的复杂度、并把定性从"数据迁移"改为"backend 替换"**。

**第二轮 7 项整改的净效果：**

| 整改项 | 对结论的影响 |
| --- | --- |
| `run_events.seq` 由 Easy → **Moderate**，且必须先实测（V3） | ⬆️ **增加**并发语义替换的工作量与不确定性 |
| Redis checkpoint cache **后移**到迁移之后（R1） | ⬆️ 迁移期**不能**用"启用缓存"来抵消 checkpoint 读路径成本 → 阶段 2 的性能风险**保持原样**（§17.2 风险 1 仍为"极高"） |
| `checkpoint_cache_db_hash()` 的身份派生（R2） | ➡️ 不阻塞迁移，但**新增一项前置修复** |
| StreamBridge durable replay 重新确认（V4） | ⬆️ 暴露一处**既有的可靠性空洞**（非迁移引入） |
| Redis 职责边界（R3） | ➡️ 不变（与上一轮结论一致，只是清单更明确） |
| Redis Stack 边界（R4） | ⬇️ **放宽**：可用 Stack 镜像部署，架构不依赖 module 即可 |
| V1/V2 升格为实施前 Gate（**V3 同属 Gate**） | ➡️ 不变，但**验收标准更硬**（V1 要求 `alembic heads` 唯一） |

**第三轮（Channel 整体删除）的净效果 —— 与第二轮方向相反：**

| 变化 | 对结论的影响 |
| --- | --- |
| 5 张渠道表（50 列）整体删除 | ⬇️ **范围缩减**：20 表 / 274 列 → **15 表 / 224 列** |
| 唯一确定性硬阻塞（`webhook_deliveries` 8448 字节主键）随表消失 | ⬇️ **原 B1 从阻塞清单移除** —— 本方案**不再有任何"建表就失败"的表** |
| 唯一 **High（最高）** 难度项（`dedupe_store` 条件 upsert）随代码删除 | ⬇️ 原 B5（`ON CONFLICT … WHERE` 重构）**整项消失** |
| `RETURNING` 5→4、`ON CONFLICT` 2→1、`make_interval` 2→0、partial unique 4→3、FK 4→2、事务级 advisory lock 3→2 | ⬇️ 并发原语改写面收窄 |
| Application Data 域非机械点由 5 个减为 3 个 | ⬇️ 该域**唯一一处难度实质下降** |
| 但 Checkpoint / Store（风险 1）与并发语义（风险 2）**完全不受影响** | ➡️ **档位不变**：三大风险里只有第 3 项被削弱 |

**第四轮（fresh-cutover + MySQL 独立链）的净效果 —— 方向是"降低迁移链复杂度"：**

| 变化 | 对结论的影响 |
| --- | --- |
| 取消 `0024_mysql_baseline`，MySQL 走**独立链**（`0001_mysql_baseline` 为链根） | ⬇️ **multiple-heads 风险消失**；"重放不适用的 PG 历史 revision"风险消失 |
| `0001_mysql_baseline` **直接体现裁剪后的 Schema** | ⬇️ **不需要 drop migration**（渠道表从不创建） |
| `_CANONICAL_0019_SCHEMA_FLOOR` / `_BASELINE_TABLE_NAMES` / `_BASELINE_INDEX_NAMES` 改判 **legacy** | ⬇️ 第三轮判定的"**唯一真实设计难点**"（常量收缩 + pin 测试语义重定义）**整项消失** |
| MySQL bootstrap 退化为 **`upgrade(head)` 单路径** | ⬇️ 不需要 `create_all` / schema-floor 校验 / forward-compatible 分支 / legacy 分支 |
| `0018` 的 PG 系统目录依赖 | ✅ **由构造消除** —— 原"需要用户确认的第一号规范偏差"**作废** |
| 确认**无历史数据迁移**、无 dual-read/write、无 backfill | ⬇️ **没有 backfill migration、没有数据校验、没有双写对账** |
| **但 Checkpoint / Store（风险 1）与并发语义（风险 2）完全不受影响** | ➡️ **档位不变** |

**第五轮（迁移范围瘦身）的净效果 —— 方向是"减少要写的东西"，不是"降低难度"：**

| 变化 | 对结论的影响 |
| --- | --- |
| **LangGraph Store 整体删除** | ⬇️ 阶段 2 从"saver + store + 二者交互"变成"**一个 saver**"；`ag_store` Schema / Store 探针 / Store 工厂分支**全部消失** |
| **删除全部历史数据库兼容设计** | ⬇️ `0002_mysql_backfill_not_null` / `0003_mysql_cascade_delete` **两条 revision 消失**；cutover 时 MySQL 链**只有 `0001_mysql_baseline`** |
| **Channel 相关 MySQL 工作删除 + 权威重算** | ⬇️ 表 20→**15 应用表**、列 274→**224**、索引 58→**47**、FK 4→**2**、partial unique 4→**3**、`RETURNING` 5→**4**、`ON CONFLICT` 2→**1** |
| **不做 Redis checkpoint cache** | ➡️ 零工作量变化（本就在范围外），且**取消了"预置 namespace / 失效 / 级联驱逐设计"的潜在新增量** |
| **只实现 async CheckpointSaver** | ⬇️ 少写一个 MySQL saver；⚠️ **但 `PyMySQL` 仍在范围内**（同步 engine 有生产消费者，§8.6） |
| **不做无关索引优化** | ⬇️ 冗余索引清理 / 查询调优 / 索引合并**全部推到上线后**（§15.7） |
| **bootstrap 不做 DB 锁** | ⬇️ 少一个并发原语；V1 的 ③ 关闭 |
| **但 CheckpointSaver 自研（风险 1）与并发语义（风险 2）完全不受影响** | ➡️ **档位不变**；**51 处 `with_for_update` / V2 / V3 数量与难度均未变** |

**一句话（五轮合并看）**：第一轮建立基线，第二轮**撤销了一处过于乐观的难度评级**，
第三轮**移走了一整块范围**，第四轮**把迁移定性为 backend 替换并收敛了迁移链的组织方式**，
第五轮**按"先删不需要的、再迁必须的"做了一次范围瘦身**。

> **档位仍是 `Feasible with significant changes`，且不能下调。**
> 缩减的是**要迁移多少东西**，不是**迁移有多难** —— 唯一可能推翻整个方案的
> **CheckpointSaver 自研**，以及**并发语义替换**这两项，**丝毫未变**。

#### （2）V1～V4 的验证结果 / 状态

| Gate | 状态 | 结论 / 待办 |
| --- | --- | --- |
| **V1** MySQL 独立链的接线方式（**第四轮重定义**；原为"`0024` 与 revision graph 的关系"。🔻**第五轮收窄**） | 🔴 **未完成 —— 实施前 Gate（必须先于 Schema 实施）** | 必须在阶段 1 关闭。**设计已定（§7.8），待落地并验证**：① 双链隔离（第二个 `script_location` + `version_table = ag_alembic_version` + head 缓存**按链做键**）；② `bootstrap_schema(..., backend="mysql")` 的 `empty`/`versioned`/`legacy→refuse` 三态。**验收：§7.8.6 的 7 条**。✅ **已收窄**：`multiple heads` 与"重放 PG 历史"两项风险**随独立链消失**；三个 bootstrap 常量**改为不动**（§7.8.4）；🔻 第五轮再缩：~~③ bootstrap 并发保护二选一~~ **已定稿为"不做 DB 锁、单一 Migrator"**（§7.8.5） |
| **V2** `checkpoint_ns` 的真实最大长度 | 🔴 **未完成 —— 实施前 Gate（必须先于阶段 2 DDL）** | 采集 Lead Agent / SubAgent / 嵌套子图的 `max` / `p95` / `p99`，据此定 `VARCHAR(N)` 并加运行时断言。`VARCHAR(255)` 目前只是**设计占位** |
| **V3** `run_events.seq` 在 MySQL 8.0.24 + READ COMMITTED 下的并发分配语义 | 🔴 **本轮新增，未实测** | **本轮最重要的一项**。已在代码层面确认"不能凭编译通过判定等价"（§13.7.1–13.7.2）；实测需覆盖 **有行 / 空 thread / 同进程 vs 跨进程**，并确认是否出现 **1062**。结论二选一：能串行化 → 保留 `with_for_update()`；不能 → **per-thread sentinel row + `SELECT … FOR UPDATE`**（推荐）或唯一约束 + bounded retry |
| **V4** Redis StreamBridge 重启 / `StreamGap` 后 SSE 是否存在 RunEventStore 自动补偿 | 🟡 **代码层面已确认"没有自动补偿"；待实测的是"是否静默继续"。🔻 第五轮：本项完全不在本次变更范围** | **不能**声称"`run_events` 会自动回源"。gap 检测只覆盖**保留窗口被裁掉**（`redis.py:291` 要求 `earliest_entries` 非空），**不覆盖 Redis 重启丢流**；恢复依赖客户端 `reload_durable_state`。🔻 **第五轮定稿**：本阶段**不做** gap 检测修补、也**不做** durable replay（§18.3）—— 它是迁移后的独立可靠性项 |

#### （3）哪些属于 MySQL 实施阻塞项

**第三轮后编号已重排**（原 B1 移除，其余顺移）：

| # | 阻塞项 | 类型 | 阻塞点 |
| --- | --- | --- | --- |
| ~~B1~~ | ~~🔴 `webhook_deliveries` 主键 8448 字节 > InnoDB 3072 上限~~ | ✅ **已移除** | 第三轮删渠道后**本项不再存在**。缩减后 15 张表**没有任何建表会失败的表**（§4.7） |
| **B1** | 🔴 **V1：MySQL 独立链的接线方式**（第四轮重定义） | **实施前 Gate** | 阶段 1。不解决会导致 **head 判定串链** / `upgrade` 跑错链 / 读到对方的 `version_table`。⚠️ **第四轮修正**：原"multiple heads"与"重放 PG 历史 revision"两个子问题**已随独立链消失**；原"三个常量如何随渠道删除收缩"的子问题**同样消失**（改为**不动**，§7.8.4） |
| **B2** | 🔴 **V2：`checkpoint_ns` 最大长度** | **实施前 Gate** | 阶段 2 的 `ag_checkpoint*` DDL |
| **B3** | 🔴 **V3：`run_events.seq` 在 MySQL + RC 下的并发分配语义** | **实施前 Gate** | 阶段 4 的 advisory-lock 替换方式与并发正确性验收 |
| **B4** | **4 处** `RETURNING` 的手工改写 + CI 静态拦截（原 5 处） | 必做项（静默陷阱） | 阶段 4（编译期不报错，必须显式防回归） |
| **B5** | ~~原为 `dedupe_store` 的条件 upsert 重构（唯一 High 项）~~ → 现为 **3 处** partial unique index → 生成列唯一索引 | 必做项（DDL 能力缺失） | 阶段 3。**原最高难度项已整项消失** |
| **B6** | 自研 MySQL **async** CheckpointSaver（🔻 第五轮：~~/ Store~~ —— Store 已整体删除，§9） | 必做项（无合规现成实现） | 阶段 2，**唯一可能推翻整个方案的点**。🔻 第五轮后**范围收窄一半**（不再含 Store），但**单点难度未变** |
| **B7** | **2 处** FK 移除后的应用层级联 + 孤儿巡检（原 4 处） | 必做项（规范禁止 FK） | 阶段 3。🔻 第五轮改判：**这是应用层代码，不占用 revision 编号**（§19 阶段 3 第 18 条） |
| **B8** | **48 个**时间列 → UTC `DATETIME(6)`（原 61 个） | 必做项（静默截断/丢时区） | 阶段 3 |
| **B9** | 🔴 **7 处 `("sqlite","postgres")` 能力闸门在 MySQL 下会拒绝启动**（第五轮新发现，§17.2 #18） | **必做项（否则启动失败）** | 阶段 1 / 阶段 5。3 处 `SystemExit`（`deps.py:84` scheduler.multi_instance、`deps.py:97` GATEWAY_WORKERS、`deps.py:132` agent_storage）+ 1 处 `RuntimeError`（`app.py:405` subagent_batches）+ 3 处 `ValueError`（`health.py:229`、`agents/__init__.py:47`、`managed_subagents/__init__.py:32`）。**逐处扩展 `Literal` 或放宽条件** |
| **B10** | 🔴 **Store 删除的 3 处非显然耦合**（第五轮新发现，§9.3） | **必做项（否则 ImportError / 启动失败）** | 阶段 6。① `runtime/store/_sqlite_utils.py` **必须保留**（被 `checkpointer/provider.py:33`、`checkpointer/async_provider.py:34`、`app/gateway/health.py:176` 复用）；② `deps.py:435` 的 `make_store(config)` **无条件构造**，删除时要同步摘掉；③ `deps.py:498` 的 `make_thread_store(sf, app.state.store)` 要改成 `make_thread_store(sf)`（DB 模式下 store 参数本就被忽略，§9.2） |
| **P1** | **前置条件（非迁移阻塞项）**：**阶段 0 的 Channel / GitHub Webhook 删除必须先完成** | **迁移的前置条件** | 它决定 Schema 范围（15 vs 20 张表）与 `0001_mysql_baseline` 的内容。⚠️ **它本身不是 MySQL 迁移的一部分**，而是一项独立的能力下线。⚠️ **第四轮修正**：三个 bootstrap 常量**不要动**（§19 阶段 0 组 B） |

#### （4）哪些只是后续性能或可靠性优化项

> 以下各项**均不阻塞迁移**，且按 §20.12 R1 的决策，**不与迁移同时进行**。
>
> 🔻 **第五轮加强**：这些项不仅"不与迁移同时进行"，而且**本次变更范围里根本不设计它们**
> —— 包括**不为它们预留任何 Schema / 配置 / 键空间结构**。
> 原因是它们的形状取决于上线后的真实访问模式，**现在设计等于凭猜测加约束**（§18.3）。

| # | 优化项 | 性质 | 前置 / 备注 |
| --- | --- | --- | --- |
| **O1** | **启用 `checkpoint_cache.type: redis` + `checkpoint_channel_mode: delta`** | **性能** | 🔻 **第五轮：整项移出本次变更范围** —— 本阶段保持 `full` + 不启用缓存 + **不做预置设计**。将来启用时先完成 **R2**（`checkpoint_cache_db_hash()` 的稳定身份派生），并作为独立变更交付 |
| **O2** | **鉴权读缓存**（`users` 行 + PAT 摘要） | **性能**（但**带安全语义**） | 必须在**所有**递增 `token_version` 的写路径显式删键，不能只靠 TTL。🔻 第五轮：仍在范围外，且**不预置** |
| ~~O3~~ | ~~**渠道会话映射缓存**~~ | ✅ **已移除** | ⛔ 随渠道删除。第 1 档因此只剩 O2 |
| **O3** | **线程 / run 列表结果缓存** | 性能 | 失效面宽，建议等压测数据 |
| **O4** | **StreamBridge durable replay fallback**（完整形态） | **可靠性** | 🔻 **第五轮：整项移出本次变更范围** —— 原计划"阶段 4 至少补 gap 检测空洞"**已取消**；完整 fallback（需把 SSE `event_id` 改为可回源到 DB 的标识，**接口级改动**）留作迁移后的独立可靠性项 |
| ~~O5~~ | ~~**"把 webhook 去重整表移出 MySQL"**（Redis `SET NX EX`）~~ | ✅ **已移除** | ⛔ 第三轮直接删除了渠道，**这张表本来就会消失**，不需要用 Redis 绕开它，也**不需要**接受"有损去重"的代价（§18.4）。这是第三轮相对"用 Redis 兜"方案的**关键优势** |
| **O5** | **Redis 部署镜像选择 / Stack 依赖核查** | 运维 | 架构只依赖 OSS 7 core commands；用 Stack 镜像部署**允许**，换回 OSS 镜像**无需改代码** |

> **不属于本表的五项**（容易混淆）：
> - **V4 的代码层面确认**（"没有自动补偿"）**已经是结论**，不是待办；🔻 第五轮后"补恢复路径"（O4）**也已移出本次范围**。
> - **沙箱 ownership store 与 volatile Redis 前提冲突**（§18.1）**不属于本迁移范围**，
>   且其持久化方案**本阶段不决策**（§20.13）—— 只锁定一条约束：
>   **不得放入"重启即空"的 volatile Redis**。
> - **阶段 0 的 Channel 删除**是**前置条件**（P1），既不是阻塞项也不是优化项 ——
>   把它混进本表会导致"迁移范围"与"能力下线"两件事互相掩盖。
> - 🔴 **已取消的 `0024_mysql_baseline` 方案**不属于本表任何一栏 ——
>   它既不是阻塞项也不是优化项，而是**被第四轮设计取代的旧方案**（§7.8.1）。
> - 🔻 **索引优化工作**（§15.7：冗余索引清理 / 查询调优 / 索引合并 / 推测性优化索引）
>   也不属于本表 —— 它不是"迁移后的优化项"，而是**明确不做的事**：
>   本次只保证"**必要索引 + 并发正确性**"，其余等 MySQL 真正跑起来后按 `EXPLAIN` 与真实负载单独优化。

#### （5）更新后的 Goal / Phase 顺序（**第五轮重排**）

> 与 §19 的阶段划分一致，此处只列**顺序与依赖**，不重复展开内容。
> 🔻 **第五轮的三处排序变化**：① 阶段 2 改名并去掉 Store；② 阶段 3 只剩一条 revision；
> ③ 阶段 4 去掉 StreamBridge 修补（V4/O4 移出范围）。

| 序 | 阶段 / Goal | 依赖 | 出口标准 |
| --- | --- | --- | --- |
| 0 | **Channel / GitHub Webhook 删除**（前置条件 P1，**非迁移本体**） | 无 | `app/channels/` 与 `gateway/github/` 已删、Gateway 可启动、三个 bootstrap 常量**未改**且 pin 测试通过、4 个 `DeltaChannel` 测试仍在且通过 |
| 1 | **确定 MySQL 独立链 + Schema / Baseline / Driver / Checkpoint Schema** | 阶段 0 | 🔴 **Gate V1 关闭**（§7.8.6 的 7 条验收，**不含** bootstrap DB 锁）；🔴 **Gate V2 关闭**（`checkpoint_ns` 长度）；驱动结论落地（`asyncmy` + `PyMySQL`）；⛔ **不设计 `ag_store`** |
| 2 | **MySQL **async** CheckpointSaver + Object Storage Blob**（最高风险，最先做） | 阶段 1 | 现有 checkpoint 回归测试通过；否则方案退化为 `Not currently recommended`。⛔ **不含 Store、不含同步 Saver** |
| 3 | **Application Data 迁移** + 写 **`0001_mysql_baseline`**（MySQL 链根，**唯一新 revision**） | 阶段 1 | 15 张应用表 / 224 列 / 47 索引 / 3 生成列唯一索引；无 FK；**无渠道表**；**无 backfill / 无级联 revision**；应用层级联删除作为代码验收 |
| 4 | **并发与 SQL 语义替换** + 多实例并发重新验证 | 阶段 3 | 🔴 **Gate V3 关闭**；四个职责域并发测试通过；无 1062 导致的事件丢失。⛔ **不含 StreamBridge 修补**（移出范围） |
| 5 | **Application DB 与 Checkpointer 分阶段切流** | 阶段 2 + 4 | 每个职责域切走后**单向不回退**（不设计数据层回滚，§20.6）；切流期 `checkpoint_channel_mode = full` |
| 6 | **彻底删除 PostgreSQL**（含 §7.8.4 legacy 清单、PG migration 链、**`runtime/store/*`**） | 阶段 5 稳定期结束 | 仓库内无 PG Driver / Saver / Schema helper / PG 链；无 `runtime/store/`（**保留 `_sqlite_utils.py`**）；无 `("sqlite","postgres")` 式闸门 |
| 7 | **（迁移之后，独立变更）Redis 侧可靠性 / 性能优化** | 阶段 6 | 不阻塞迁移，**不与迁移同时进行**，且**本次变更不为它们做任何预置设计** |

**核心原则不变**：先做 Checkpoint（阶段 2，最高风险），再做 Application Data（阶段 3），
最后做并发语义替换与切流（阶段 4/5）。
**前置例外**：阶段 0 与 Gate V1 / V2 / V3 必须在对应阶段开工前关闭。
🔻 **第五轮补充**：阶段 0（删渠道）与阶段 6（删 Store）都是"**删除**"而非"迁移"，
体现的是同一条原则 —— **先把不需要的删掉，再迁移剩下的**。

#### （6）是否还有需要确认的架构决策

> **结论：本轮之后，没有"待业务确认"的架构决策**，只剩一处**规范偏差需要组织层面拍板**。

| 事项 | 状态 |
| --- | --- |
| Channel / GitHub Webhook 范围 | ✅ **已确认**（第三轮：**整体删除**，沿用 `feature-inventory`） |
| Sandbox ownership 的 Redis 持久化 | ✅ **已确认**（第三轮：**本阶段不决策**，只锁约束） |
| 迁移定性（fresh-cutover / 无历史数据 / 无 dual-read-write / 无 backfill / 不支持原地升级） | ✅ **已确认**（第四轮） |
| MySQL 独立 migration chain、`0001_mysql_baseline` 为链根 | ✅ **已确认**（第四轮） |
| `bootstrap.py` 三个常量标记 legacy、**一个字都不动** | ✅ **已确认**（第四轮，§7.8.4） |
| 🔻 **LangGraph Store 整体删除**（含 `runtime/store/*`、`ag_store`、Store 探针/工厂分支） | ✅ **已确认**（第五轮）。⚠️ **三个非显然耦合必须处理**（§9.3 / B10） |
| 🔻 **删除全部历史数据库兼容设计**（`0024`、`0002` backfill、`0003` 级联、回滚路径） | ✅ **已确认**（第五轮）。MySQL 链在 cutover 时**只有 `0001_mysql_baseline`** |
| 🔻 **不做 Redis checkpoint cache**（含不预置 namespace / 失效 / 级联驱逐设计） | ✅ **已确认**（第五轮，§18.3 / §20.12 R1） |
| 🔻 **不做无关索引优化**（只保证必要索引 + 并发正确性） | ✅ **已确认**（第五轮，§15.7） |
| 🔻 **bootstrap 不做 DB 锁**（单一 Migrator 先迁移、再启动多实例） | ✅ **已确认**（第五轮，§7.8.5）—— 该行由上一版的"🟡 属 V1 工程选择"**升级为已确认** |
| 🔻 **同步路径的保留范围**：同步 **Saver 不迁移**（无生产消费者），但同步 **driver（`PyMySQL`）必须保留** | ✅ **已确认**（第五轮，§8.6）—— ⚠️ 这是最容易被合并成"同步路径整体不做"的一处，**不要合并** |
| **migration 载体保持 Alembic Python revision（不改成裸 SQL）** | 🟡 **本轮唯一保留的规范偏差**（§7.7）—— **建议接受**；若组织要求裸 SQL，则需重写整个 bootstrap 层，属**方案级变更** |
| 🔻 **是否进一步删除 `mcp_tasks` / `subagent_batches` / `subagent_batch_items`**（合计 **85 列 = 38%** 的表面积） | 🟡 **需要用户决策**（第五轮提出，§1.7 E）。事实：三者**当前均 `enabled: false`**；但 `feature-inventory` Rev.3 把 `mcp_tasks`（B4）与 `subagent_batches`（B5/B24）列为 **B 类"待评估启用"**，明确"**不要随意禁用**" ⇒ **本次不擅自删除**，只作为"可进一步瘦身"的候选上报 |
| V1–V4 | ✅ 均为**工程调查 / 实测项**，**不需要业务确认**（第三轮已明确）。🔻 第五轮：V1 收窄（③ 已关闭）、V4 移出本次范围 |

**因此：除"migration 载体是否为 Alembic Python revision"这一处规范偏差需要组织层面拍板外，
只剩一项新的业务决策 —— 是否进一步删除 3 张 `enabled: false` 的 B 类能力表（85 列）。**
其余架构决策均已确认。

---

## 附录 A：本次调查使用的证据清单

| 类别 | 路径 / 方法 |
| --- | --- |
| 依赖实测 | `backend/.venv/bin/python -c "importlib.metadata.version(...)"`（`langgraph` 1.2.9 / `langgraph-checkpoint-postgres` **3.1.1 已安装** / `sqlalchemy` 2.0.49 / `asyncpg` 0.31.0 / `psycopg` 3.3.3） |
| LangGraph 后端能力 | `ls .venv/lib/python3.12/site-packages/langgraph/checkpoint/`、`ls .../langgraph/store/`、`grep -rli mysql .venv/.../langgraph/`（零命中） |
| PG saver DDL/SQL | `.venv/.../langgraph/checkpoint/postgres/base.py:47-123,195-202` |
| **Schema 转储** | `/tmp/schema_dump.py`（SQLAlchemy 反射 `Base.metadata` + `CreateTable().compile(dialect=mysql/postgresql)`），输出 `/tmp/schema_out.txt` |
| **方言编译探针** | `/tmp/dialect_probe.py`：`UPDATE…RETURNING`、`with_for_update(skip_locked/read)`、`SELECT max(…) FOR UPDATE`、`JsonMatch`、`DATETIME` fsp、键长估算 |
| 配置模型 | `config/database_config.py:142-209`、`config/checkpointer_config.py:9-32`、`config/object_storage_config.py`、`config/app_config.py:323-329,473-507,509-519` |
| PostgreSQL 适配层 | `persistence/postgres_schema.py`、`persistence/engine.py:30-50,58-92,117-131,169-233`、`persistence/bootstrap.py:106-266,290-327,361,416,439-505,537-561` |
| ORM 模型 | `persistence/base.py` + 15 个 `*/model.py`（**20 张表 / 274 列**；第三轮删除 5 张渠道表后为 **15 张应用表 / 224 列**。🔻 **第五轮权威重算**：20 张 = **15 张应用表 + 5 张框架表**（checkpoint 4 + migration 版本表 1）；索引 **47** 个（`ix_` 42 / `idx_` 1 / `uq_` 4）、`sa.JSON` **20** 个、`DateTime(timezone=True)` **48** 个、FK **2**、partial unique **3**、`RETURNING` **4**、`ON CONFLICT` **1**、事务级 advisory lock **2**、`with_for_update` **51**（**未变**）。逐表列数由 `/tmp/schema_dump.py` 实测确认：`mcp_tasks` 45、`runs` 31、`subagent_batch_items` 23、`scheduled_tasks` 23、`subagent_batches` 17、`scheduled_task_runs` 16、`channel_connections` 16、`channel_oauth_states` 11、`threads_meta` 10、`run_events` 10、`users` 9、`personal_access_tokens` 9、`channel_credentials` 9、`channel_conversations` 9、`projects` 8、`feedback` 8、`agents` 7、`webhook_deliveries` 5、`managed_subagents` 5、`user_preferences` 3） |
| Migration（PG 链，**保持 immutable、不再被 MySQL 引用**） | `persistence/migrations/versions/0001_baseline.py` … `0023_user_preferences.py`（24 个文件）、`migrations/env.py:92-101`、`migrations/_helpers.py:31-128`、`migrations/_env_filters.py:28-35`、`migrations/AGENTS.md:118-142`（**含 `:128` 的"独立 alembic chain + 独立 `version_table`"先例**） |
| **Bootstrap（第四轮重点）** | `persistence/bootstrap.py`（700 行）：三分支状态机、`_MIGRATIONS_DIR`（**单 `script_location`**）、`_HEAD_REVISION` / `_KNOWN_REVISIONS` **模块级缓存**、`_get_head_revision()`（`get_current_head()` 为 `None` 时抛 `RuntimeError`）、`_CANONICAL_0019_SCHEMA_FLOOR:134-171`、`_BASELINE_TABLE_NAMES:198-210`、`_BASELINE_INDEX_NAMES:215-249`、`_FORWARD_COMPATIBLE_REVISION`、`_validate_forward_schema()`、`_run_create_all_sync()` / `_run_baseline_create_all_sync()`、`_read_database_revision()`（要求**恰好一行**）、`_get_alembic_config()`（`version_table` **当前未设置**）、`_postgres_lock()` / `_PG_LOCK_KEY`、`_sqlite_lock()`、`_bootstrap_lock()`（未知 backend 抛 `ValueError`）、`bootstrap_schema(engine, *, backend, postgres_schema="")` |
| Checkpointer | `runtime/checkpointer/{provider,async_provider}.py`、`runtime/checkpointer/cached_saver.py`、`checkpoint_patches.py` |
| Store（🔻 **第五轮重查：结论是"整体删除"**） | `runtime/store/{provider,async_provider,_sqlite_utils}.py`（`make_store` / `get_store` / `reset_store` / `store_context`）；唯一真实消费者 `persistence/thread_meta/memory.py:25-27` 的 `MemoryThreadMetaStore`；构造点 `app/gateway/deps.py:435`（**无条件构造**）与 `:498`；5 处 `store=` 挂载点 `deps.py:716` / `worker.py:806,1257` / `checkpoint_state.py:129` / `services.py:1195`（**全部只挂载、不读写**）；`app.py:129-171` 的孤儿线程迁移是唯一真实 `asearch(("threads",))` 调用方 |
| **能力闸门（第五轮新查）** | 7 处 `("sqlite","postgres")` 判定：`deps.py:84`（scheduler.multi_instance → `SystemExit`）、`deps.py:97`（GATEWAY_WORKERS → `SystemExit`）、`deps.py:132`（agent_storage → `SystemExit`）、`app.py:405`（subagent_batches → `RuntimeError`）、`health.py:229`、`persistence/agents/__init__.py:47`、`persistence/managed_subagents/__init__.py:32`（→ `ValueError`） |
| **同步 / 异步消费者（第五轮新查）** | `persistence/agents/sql.py:45-52`（**同步** `create_engine` + `_engines` 缓存，由 `make_agent_store` 构造，docstring 明示运行在 **graph subprocess**）；`client.py:145,471-475,618-624,918-922`（`DeerFlowClient`，同步 API，**在 `app/` / `packages/` 零生产引用**）；`runtime/checkpointer/provider.py:103,115-143`（同步 Saver 分支） |
| 并发原语 | `runtime/events/store/db.py:110-190`、`persistence/run/sql.py:540-640`、`persistence/scheduled_task_runs/sql.py:195-220,300-330,750-770`、`persistence/scheduled_tasks/sql.py`、`persistence/mcp_tasks/sql.py:158-175,220-240,370-380,470-490`、`persistence/subagent_batches/sql.py:240-330`、`persistence/user/preferences.py:4-25`<br>~~`persistence/channel_connections/sql.py:395-405`、`app/channels/dedupe_store.py:30-230`~~ ⛔ **第三轮已移出范围**（渠道删除） |
| JSON 方言 hack | `persistence/json_compat.py:225-231` |
| **对象存储（HEAD 新增）** | `object_storage/{port,keys,outputs,uploads,__init__}.py`、`config/object_storage_config.py` |
| 健康检查 | `app/gateway/health.py:71-260` |
| 多实例闸门 | `app/gateway/deps.py:49-113,388-391` |
| 部署 | `deploy/helm/deer-flow/values.yaml:119-160,185-216`、`docker/*.yaml`（无 postgres）、`config.example.yaml:1794-1820,1877-1886` |
| 第三方 MySQL 包 | PyPI `https://pypi.org/pypi/langgraph-checkpoint-mysql/json`（3.0.0，2026-01-23，作者 Theodore Ni，`requires_dist` 实测） |
| 既有相关分析 | `docs/architecture/redis-checkpoint-store-feasibility.md`（Redis 后端评估）、`docs/architecture/phase5-*.md` |
| 测试 | 🔻 **113 个**命中 `postgres` 的 `.py` 文件（第五轮重算；原文档记 111。分布：`packages/harness/deerflow` 48 + `app/` 8 + `tests/` 57）；8 个 PG 专属文件（清单见 §3.7）；14 个渠道测试文件（随渠道删除） |
| 🔻 **第五轮的重算手段** | ① 重新反射 `Base.metadata`（`cd backend && .venv/bin/python /tmp/schema_dump.py`）逐项重数列 / 类型 / 约束 / 索引；② 全仓 `Grep`（ripgrep）统计 `postgres` / `with_for_update` / `RETURNING` / `ON CONFLICT` / `advisory` 的出现面；③ **逐个读取** `runtime/store/*`、`persistence/thread_meta/*`、`persistence/agents/*`、`client.py`、`deps.py` 的调用关系以确认消费者；④ 对 7 处 `("sqlite","postgres")` 判定逐处核对失败语义 |

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
- ✅ 仅执行了只读代码搜索、依赖清单核对、已安装包版本探测、
  **SQLAlchemy 方言编译探针**（脚本置于 `/tmp`，未写入仓库、未连接任何数据库），
  以及从 PyPI 获取元数据（未下载安装到项目环境）
