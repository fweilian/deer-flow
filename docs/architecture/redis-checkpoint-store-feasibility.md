# Redis 作为 Checkpoint / Store 后端的可行性分析

> 审计口径：只读。所有结论基于 **本机实际安装的包** 与 **从 PyPI 解包到 `/tmp` 阅读的源码**，
> 不采信 README 与文档声明。证据一律给 `文件:行号`。
>
> 审计日期：2026-09-16

---

## 1. Executive Summary

### 1.1 一句话结论

**技术上可行，工程上不划算。当前不推荐作为生产默认路径。**

Redis 在本项目里**不是新引入的组件** —— `stream_bridge`（跨进程 SSE）与
`checkpoint_cache`（delta 历史缓存）已经跑在 Redis 上，`redis>=5.0.0` 已在依赖里
（`backend/pyproject.toml:39,64`、`packages/harness/pyproject.toml:66`）。
本任务问的是**把 checkpoint 与 store 的持久化后端也搬到 Redis**，这与前两者性质完全不同：
前两者是**可重建的辅助设施**（缓存丢了只影响性能，流断了客户端重连），
而 checkpoint 是**会话状态的唯一真相**。把唯一真相放进一个默认配置下会静默淘汰数据的内存数据库，
风险等级不是同一档。

### 1.2 分章判定

| 职责域 | 判定 | 核心阻碍 |
| --- | --- | --- |
| **LangGraph Checkpoint** | **Feasible with significant changes** | 官方无实现，须引第三方 `langgraph-checkpoint-redis`；该包**硬依赖 RedisJSON + RediSearch 模块**，现有 `redis:7-alpine` 镜像不满足；`alist(before=...)` 游标在 langgraph 生成的 ID 格式下**静默失效**；打破"`database.backend` 单一后端"配置模型 |
| **LangGraph Store** | **Feasible with significant changes** | 同上需第三方 `langgraph.store.redis`；索引 schema 不覆盖业务 metadata，`filter` 是**取页后在 Python 里做的**，导致带过滤的分页**静默不完整**；且 Store 实际使用面极窄，收益接近零 |
| **Application Data（ORM）** | **不受影响，也不应受影响** | 23 个 alembic 迁移、26 张关系表、advisory lock / `SKIP LOCKED` / partial unique index。Redis 不是这个职责的候选，见 §7 |
| **Agent Memory（长期记忆）** | **完全无关** | `deermem` 走本地文件 + SQLite FTS5，不经过 `database.backend` |
| **Stream Bridge / Checkpoint Cache** | **已在 Redis 上，保持现状** | 见 §3 |

### 1.3 结论何时退化

- 若组织**不能接受**长期维护一个针对 `langgraph-checkpoint-redis` 的本地补丁
  （`before` 游标修复），Checkpoint 判定应退到 **Not currently recommended**。
- 若目标 Redis 实例是**社区版**（无 RedisJSON / RediSearch），判定直接为
  **Not currently recommended** —— 这不是"改配置"，是换产品线。
- 若允许 `maxmemory-policy` 设为任何 `*-lru` / `volatile-*`，判定为 **Not currently recommended**：
  Redis 会静默删除 checkpoint，而应用层不会收到任何错误。

---

## 2. 审计基线

### 2.1 版本（实测，非声明）

| 包 | 版本 | 证据 |
| --- | --- | --- |
| `langgraph` | **1.2.9** | `packages/harness/pyproject.toml:29` `>=1.2.9,<1.3`；`.venv` 实测 |
| `langgraph-checkpoint` | **4.1.1** | `.venv/…/langgraph_checkpoint-4.1.1.dist-info` |
| `langgraph-checkpoint-postgres` | **3.1.1**（已安装） | `.venv` 实测 |
| `langgraph-checkpoint-sqlite` | **3.1.1**（已安装） | `.venv` 实测 |
| `langgraph-api` | **0.10.0** | `.venv` 实测 |
| `redis` | **7.4.0**（已安装） | `backend/pyproject.toml:64` dev 组；`.venv` 实测 |
| `langgraph-checkpoint-redis` | **未安装、未声明** | 全仓库零命中 |

**已安装的 LangGraph 后端**（`ls .venv/…/langgraph/checkpoint/`）：

```
base  memory  postgres  serde  sqlite
```

`ls .venv/…/langgraph/store/`：`base  memory  postgres  sqlite`

→ **官方没有 Redis checkpointer，也没有 Redis store。** 这与官方没有 MySQL 后端是同一类事实。

### 2.2 当前实际部署形态

`config.yaml:209-219`：

```yaml
database:
  backend: postgres
  postgres_url: $DATABASE_URL
  checkpoint_channel_mode: full
```

即 **checkpoint 与 store 都由 `database.backend` 驱动，与 ORM 共用 Postgres**。

两条构建路径（都提供）：

| 路径 | 实现 | 位置 |
| --- | --- | --- |
| 异步（Gateway 主路径） | `AsyncPostgresSaver` + `psycopg_pool.AsyncConnectionPool` | `runtime/checkpointer/async_provider.py:129-140` |
| 同步（TUI / CLI / `DeerFlowClient`） | `PostgresSaver.from_conn_string` | `runtime/checkpointer/provider.py:132-147` |
| Store（异步） | `AsyncPostgresStore` | `runtime/store/async_provider.py:80-95` |
| Store（同步） | `PostgresStore` | `runtime/store/provider.py:128-143` |

---

## 3. Redis 在本项目中的既有存在（重要前提）

Redis **已经**是本项目的正式组件，只是承担的不是持久化职责：

| 用途 | 实现 | 配置键 | 性质 |
| --- | --- | --- | --- |
| 跨进程 SSE 流桥 | `runtime/stream_bridge/redis.py`（16 KB，Redis Streams） | `stream_bridge.type: redis` | **可重建**：流丢了客户端重连 + `gap` 事件 |
| Delta 历史缓存 | `runtime/checkpoint_cache/redis.py` | `database.checkpoint_cache.type: redis` | **性能专用**：`RedisCheckpointHistoryCache` 的 `aget_many`/`aset_many` 失败只记 warning 并当作 miss（`checkpoint_cache/redis.py:68-72, 92-94`） |

这两处的共同设计前提是 **"Redis 挂了不影响正确性"** —— 缓存代码显式写明
"a redis outage costs hits, never availability"（`checkpoint_cache/redis.py:69`）。

**这个前提在 checkpoint 作为主存后不成立**，是本报告最重要的判断依据。

部署现状（`docker/docker-compose.yaml:29-36`、`docker-compose-dev.yaml:40-47`）：

```yaml
redis:
  image: redis:7-alpine
  command: ["redis-server", "--appendonly", "yes"]
```

**这是纯 Redis 7，不含 RedisJSON、不含 RediSearch。** 见 §4.3。

---

## 4. Checkpoint 分析

### 4.1 唯一可用的第三方实现

`langgraph-checkpoint-redis`，PyPI 最新 **0.5.2**，作者组织 `redis-developer/langgraph-redis`（Redis Inc.），
**不是 LangChain/LangGraph 官方组件**。

依赖声明（PyPI metadata 实测）：

```
langgraph-checkpoint >=4.1.1,<5.0.0     ← 项目现有 4.1.1 ✓
orjson >=3.9.0                          ← 新增
redis >=5.2.1                           ← 项目现有 7.4.0 ✓
redisvl >=0.15.0,<1.0.0                 ← 新增（大件）
tomli >=2.0.1 (py<3.11)                 ← 不适用
```

`redisvl` 0.27.2 自身的依赖：

```
jsonpath-ng>=1.5.0, ml-dtypes<1.0.0,>=0.4.0, numpy<3,>=1.26.0,
pydantic<3,>=2, python-ulid>=3.0.0, pyyaml<7.0,>=5.4,
redis!=8.0.0,<9.0,>=6.3.0, tenacity>=8.2.2
```

→ 新增传递依赖至少 **`redisvl` / `orjson` / `python-ulid` / `numpy` / `ml-dtypes` / `jsonpath-ng`**。
其中 `numpy` 是本项目此前不依赖的重量级组件。

### 4.2 提供的模块

解包 wheel 后的目录树：

```
langgraph/checkpoint/redis/{__init__,aio,base,ashallow,shallow,jsonplus_redis,key_registry,util,message_exporter}.py
langgraph/store/redis/{__init__,aio,base,token_unescaper,types}.py
langgraph/middleware/redis/{conversation_memory,semantic_cache,semantic_router,tool_cache,vectorizer}.py
```

代码规模：checkpoint 侧 ~8.5 kLOC，store 侧 ~2.5 kLOC。
`langgraph/middleware/redis/*`（语义缓存、语义路由、对话记忆）本任务**不需要**。

### 4.3 硬依赖：RedisJSON + RediSearch（部署阻碍）

包的 METADATA 明写（`langgraph_checkpoint_redis-0.5.2.dist-info/METADATA:62-79`）：

> **IMPORTANT:** This library requires Redis with the following modules:
> - **RedisJSON** - For storing and manipulating JSON data
> - **RediSearch** - For search and indexing capabilities
> …Failure to have these modules available will result in errors during index creation and checkpoint operations.

源码层面同样成立 —— 全部索引通过 `redisvl` 的 `SearchIndex` 建立：

- `langgraph/checkpoint/redis/__init__.py:23` `from redisvl.index import SearchIndex`
- `langgraph/checkpoint/redis/__init__.py:128-133` `create_indexes()` 建两个索引
- `langgraph/checkpoint/redis/base.py:755` 直接用 `JSON.SET`
- `langgraph/checkpoint/redis/aio.py:1446` `self._redis.json().get(...)`

**这意味着：**

| 目标实例 | 能否用 |
| --- | --- |
| 本项目 compose 的 `redis:7-alpine` | ❌ **不能**。无 JSON / Search 模块，`FT.CREATE` 直接失败 |
| `redis/redis-stack-server`（Redis Stack） | ✅ |
| Redis 8.0+ 官方镜像 | ✅（METADATA:69 明写 8.0+ 模块已进核心） |
| 云厂商**社区版** Redis（阿里云/腾讯云标准版等） | ❌ 通常不含这两个模块 |
| 云厂商**企业版 / 增强型 / Tair** | ⚠️ 需逐个确认模块清单与版本 |

这一条单独就足以把结论压到"需要换基础设施"这一档 —— 不是配置改动，是**换 Redis 产品线**。

### 4.4 API 契约核对

把 Redis saver 的方法集与**现役的** `AsyncPostgresSaver` 逐项对照：

| 方法 | `AsyncPostgresSaver` 3.1.1 | `AsyncRedisSaver` 0.5.2 | 备注 |
| --- | --- | --- | --- |
| `setup()` | ✓ `aio.py:90` | ✓ `aio.py:267`（转 `asetup`） | 契约一致 |
| `get_tuple` / `list` | ✓ | ✓ | |
| `put` / `put_writes` | ✓ | ✓ | |
| `delete_thread` | ✓ `aio.py:340` | ✓ `aio.py:1945` | DeerFlow 唯一实际调用的删除方法 |
| `prune` | ✗ 基类 `NotImplementedError` | ✓ `aio.py:2038` | Redis **更强** |
| `delete_for_runs` | ✗ 基类 `NotImplementedError` | ✗ 基类 `NotImplementedError` | **两边都缺，非回归** |
| `copy_thread` | ✗ 基类 `NotImplementedError` | ✗ 基类 `NotImplementedError` | 同上 |
| `aget_delta_channel_history` | ✓ **覆写** `aio.py:405` | ✗ 继承基类 | delta 模式下 Redis 走通用 O(祖先数) 遍历 |

基类行为证据：`langgraph/checkpoint/base/__init__.py:331-348`（`delete_for_runs`）、
`:350-372`（`copy_thread`）、`:522-538` / `:540-558`（异步版）全部 `raise NotImplementedError`。

**DeerFlow 对 checkpointer 的实际调用面很窄** —— 全仓库只有一处删除调用：

```python
# app/gateway/routers/threads.py:751-752
if hasattr(checkpointer, "adelete_thread"):
    await checkpointer.adelete_thread(thread_id)
```

以及 `CachedHistorySaver` 的委托方法（`runtime/checkpointer/cached_saver.py:255-328`）。
其中 `delete_for_runs` / `copy_thread` / `prune` 在**生产路径上无调用者**
（`tests/test_cached_history_saver.py:191` 的注释也确认 "base-class no-op"）。

**结论：API 契约层面 RedisSaver 不构成阻碍。** 唯一的软性退化是
`aget_delta_channel_history` 未覆写 —— delta 模式下 Postgres 有单查询覆写，
Redis 走基类的逐祖先 `aget_tuple` 遍历，每次一次网络往返。功能正确，性能需实测。

### 4.5 发现 1：`alist(before=...)` 游标静默失效 ⚠️ 高

**这是本次审计最严重的发现。**

Redis saver 对 `before` 的实现（`langgraph/checkpoint/redis/aio.py:670-682`，
同步版同构于 `__init__.py:275-285`）：

```python
before_checkpoint_id = get_checkpoint_id(before)
if before_checkpoint_id:
    try:
        before_ulid = ULID.from_str(before_checkpoint_id)
        before_ts = before_ulid.timestamp
        # Use numeric range query: checkpoint_ts < before_ts
        filter_expression.append(Num("checkpoint_ts") < before_ts)
    except Exception:
        # If not a valid ULID, ignore the before filter
        pass
```

**链路验证：**

1. `ULID.from_str` → `base32.decode`，实现为（`python-ulid` 4.0.1，`ulid/base32.py:194-196`）：

   ```python
   def decode(encoded: str) -> bytes:
       if len(encoded) != constants.REPR_LEN:
           raise ValueError("Encoded ULID has to be exactly 26 characters long.")
   ```

2. langgraph 生成的 `checkpoint_id` 是 **UUIDv6**（`langgraph/pregel/_checkpoint.py:31`
   `id=str(uuid6(clock_seq=-2))`），本机实测：

   ```
   1f1b1cc1-8482-6a54-bffe-20bb64e32f43    # 36 字符，含连字符
   ```

3. 36 ≠ 26 → `ValueError` → 被裸 `except Exception` 吞掉 → **`before` 过滤条件根本不进查询**。

**对照 Postgres 的正确实现**（`langgraph/checkpoint/postgres/base.py:591-594`）：

```python
if before is not None:
    wheres.append("checkpoint_id < %s ")
    param_values.append(get_checkpoint_id(before))
```

字符串比较，而 uuid6 的字典序**恰好等于时间序** —— 正确且精确。

**后果：** 传 `before` 时 Redis 返回的是**最新一页**而不是"比锚点更旧的一页"。
调用方若用它翻页，会**重复拿到同一批 checkpoint**，而不是报错。

**本项目受影响的位置**（`app/gateway/services.py:1120-1146`）：

```python
if config.get("configurable", {}).get("checkpoint_id"):
    before = config
    ...
    anchor = await self.checkpointer.aget_tuple(before)
    if anchor is not None:
        result.append(_RawCheckpointSnapshot(config, anchor))
if limit is None or len(result) < limit:
    remaining = None if limit is None else limit - len(result)
    async for tup in self.checkpointer.alist(walk_config, before=before, limit=remaining):
```

`_RawCheckpointReadAccessor` 是 **full 模式下 agent factory 不可用时的降级读路径**
（`runtime/AGENTS.md` 有明确记载）。它依赖 `before` 把游标前移；`before` 失效后
这段会把锚点页当成后续页重复返回。

**影响面界定（不夸大）：** 主路径不受影响 —— `runtime/checkpoint_state.py:170,182` 的
`get_state_history(prepared, limit=limit)` **不传 `before`**，而 Pregel 只在调用方显式传 `before`
时才把它下推给 `alist`（`langgraph/pregel/main.py:1526-1528`）。
全仓库 `before=` 传参只有 `services.py:1142` 这一处（`deps.py:570` 的 `before=now_iso()` 属 run lease，无关）。

**这是"静默坏数据"类缺陷，不是崩溃** —— 与 skill 里强调的必须逐条列出的隐性约束同类。
若决定采用 Redis checkpointer，**必须**先修此处或给上游提 issue。

### 4.6 发现 2：`filter` 能力收窄

Redis saver 的 `alist` 只认 4 个 filter key（`aio.py:660-671`）：

```python
for k, v in filter.items():
    if k == "source": ...
    elif k == "step": ...
    elif k == "thread_id": ...
    elif k == "run_id": ...
    else:
        raise ValueError(f"Unsupported filter key: {k}")
```

Postgres 侧是 `metadata @> %s`（JSONB containment，`postgres/base.py:584-586`），
可对任意 metadata 键过滤。

DeerFlow 当前**不传 filter**，所以不是阻碍；但这是一条能力天花板，
未来任何"按 metadata 过滤历史 checkpoint"的需求在 Redis 上会直接抛错。

### 4.7 发现 3：未指定 limit 时存在 10000 硬上限

`aio.py:695-701`：

```python
query = FilterQuery(
    filter_expression=combined_filter,
    ...
    num_results=limit or 10000,
    sort_by=("checkpoint_id", "DESC"),
)
```

Postgres 只在 `limit is not None` 时才加 `LIMIT %s`（`postgres/aio.py:144-146`），无上限。

DeerFlow 的 `limit=None` 语义是**"不限"**（`runtime/AGENTS.md` 明确：
"`None` means unlimited — do not pass `limit=0` through"）。
在 Redis 上，`None` 会**静默变成 10000**。full 模式下一个 super-step 会产生多个 checkpoint，
长会话累计超过 10000 条是可达到的。

### 4.8 发现 4：存储形态改变 → 跨后端迁移不是数据拷贝

`_dump_checkpoint`（`langgraph/checkpoint/redis/base.py:401-433`）：

```python
type_, data = self.serde.dumps_typed(checkpoint)
if type_ == "json":
    checkpoint_data = cast(dict, orjson.loads(data))
else:
    checkpoint_data = ...
    if type_ == "msgpack":
        checkpoint_data = cast(dict, self._msgpack_to_redis_json(checkpoint_data))
...
return {"type": type_, **checkpoint_data, "pending_sends": []}
```

即 **checkpoint 被反序列化后重新编码成一个原生 RedisJSON 文档**，而不是像 Postgres 那样
把 msgpack 字节塞进 `BYTEA`。

| | Postgres | Redis |
| --- | --- | --- |
| `checkpoints` 行 | `checkpoint JSONB` + `metadata JSONB` | RedisJSON 文档（结构化） |
| 通道值 | `checkpoint_blobs.blob BYTEA`（msgpack） | 内联在文档里；二进制走 **base64**（`base.py:626-630`） |

**推论：**

1. **PG → Redis 的迁移必须走 saver API**（读 `aget_tuple` → 写 `aput`），
   不能像同构数据库之间那样直接搬数据。反向同理。
2. **没有"改 DSN 回滚"这条路** —— 两边的物理表示不同构，回滚同样需要一次 API 级重写。
3. 二进制数据（state 里的 bytes）base64 后**体积 +33%**，直接放大内存占用。

### 4.9 DeltaChannel 支持（好消息）

`JsonPlusRedisSerializer` 显式处理了 `_DeltaSnapshot`：

- `langgraph/checkpoint/redis/jsonplus_redis.py:111-113`、`:171-187`、`:411-424`
- `langgraph/checkpoint/redis/base.py:493` 注释 "Delegate `_DeltaSnapshot` markers to the serializer (issue #201)"

序列化契约也与现有包装层兼容：`dumps_typed` 仍返回 `(tag, bytes)`（`jsonplus_redis.py:240-263`），
`JsonPlusRedisSerializer` 是 `JsonPlusSerializer` 的**子类**（`jsonplus_redis.py:52`）。

因此 `CachedHistorySaver`（`runtime/checkpointer/cached_saver.py:56` `self.serde = inner.serde`）
与 `RedisCheckpointHistoryCache`（用 `serde.dumps_typed` 打 tag）**可以原样复用**。

`setup()` / `asetup()` 语义对齐（`aio.py:248-279`）：捕获运行 loop、`create_indexes()`、
`FT.CREATE ... overwrite=False`、探测 cluster 模式。与现有 `saver.setup()` 调用点一致。

---

## 5. Store 分析

### 5.1 三个 "Memory" 概念（沿用既有结论）

`docs/architecture/mysql-migration-feasibility.md` §9.1 已把三者分开，此处不重复，只补 Redis 视角：

| # | 名称 | 与 Redis 的关系 |
| --- | --- | --- |
| 1 | **LangGraph Store**（`BaseStore`） | 候选替换对象，见下 |
| 2 | **Agent Memory**（`deermem`，本地文件 + SQLite FTS5） | **无关** |
| 3 | **Thread metadata store** | `MemoryThreadMetaStore` 是 Store 的消费者，见 §5.3 |

### 5.2 Store 的真实使用面（很窄）

装配链：`app/gateway/deps.py:435` → `app.state.store` → 两个消费者：

```python
# app/gateway/deps.py:498
app.state.thread_store = make_thread_store(sf, app.state.store)
```

```python
# packages/harness/deerflow/persistence/thread_meta/__init__.py:32-45
def make_thread_store(session_factory, store=None) -> ThreadMetaStore:
    if session_factory is not None:
        return ThreadMetaRepository(session_factory)   # ← postgres/sqlite 走这条
    if store is None:
        raise ValueError(...)
    return MemoryThreadMetaStore(store)                # ← 只有 memory 后端走这条
```

```python
# packages/harness/deerflow/runtime/checkpoint_state.py:127-129
graph.checkpointer = checkpointer
if store is not None:
    graph.store = store
```

→ **`database.backend=postgres` 时，`app.state.store` 被构建（`AsyncPostgresStore`）但功能上几乎不用**：
thread 元数据走 `ThreadMetaRepository`（SQL），Store 只作为图节点可访问的命名空间挂上去。

**这是 Store 章节最关键的一条：把 Store 搬到 Redis，收益接近零。**

### 5.3 发现 5：`filter` 是取页后做的 → 带过滤的分页静默不完整 ⚠️ 中高

`RedisStore` 的索引 schema（`langgraph/store/redis/aio.py:118-140`）只声明：

```python
"fields": [
    {"name": "prefix", "type": "text"},
    {"name": "key", "type": "tag"},
    {"name": "created_at", "type": "numeric"},
    {"name": "updated_at", "type": "numeric"},
    {"name": "ttl_minutes", "type": "numeric"},
    {"name": "expires_at", "type": "numeric"},
]
```

**没有任何业务 metadata 字段被索引。**

查询构造也只看 namespace（`langgraph/store/redis/base.py:575-598`）：

```python
for idx, op in search_ops:
    filter_conditions = []
    if op.namespace_prefix:
        prefix = _namespace_to_text(op.namespace_prefix)
        filter_conditions.append(f"@prefix:{prefix}*")
    ...
    query = " ".join(filter_conditions) if filter_conditions else "*"
```

`op.filter` 直到**结果取回之后**才在 Python 里筛（`store/redis/aio.py:851-852` 向量分支、
`:924-925` 普通分支）：

```python
# Regular search
query = Query(query_str).paging(offset, limit)
res = await self.store_index.search(query)
for doc in res.docs:
    data = json.loads(doc.json)
    if op.filter:
        ...
        if not matches:
            continue          # ← 页已经取完了才丢
```

**问题：`offset`/`limit` 在过滤之前生效。** 带 filter 的分页会
**在过滤后返回不足 `limit` 条、甚至 0 条**，而实际匹配的文档可能还有很多。

**本项目受影响的位置**（`persistence/thread_meta/memory.py:132-154`）：

```python
while True:
    page = await self._store.asearch(
        THREADS_NS,
        filter=filter_dict or None,       # {"status": ..., "user_id": ...}
        limit=SEARCH_PAGE_SIZE,           # 500
        offset=search_offset,
    )
    if not page:
        break
    items.extend(page)
    if len(page) < SEARCH_PAGE_SIZE:
        break                             # ← 短页即认为扫完
    search_offset += len(page)
```

过滤后页面变短 → **循环提前 break → 用户线程列表被静默截断**。

注意界定：**这不是越权**（`user_id` 的后置过滤仍然生效，不会返回别人的 thread），
是**结果不完整且依赖全局文档顺序**。但它的表现是"用户看不到自己的部分线程"，
排查成本高，属于典型的静默坏数据。

（该路径当前只在 `database.backend=memory` 时启用；一旦引入 `backend=redis`，
ORM engine 不存在 → `sf is None` → `MemoryThreadMetaStore` 会成为**实际生效**的实现。）

### 5.4 其余差异

- `RedisStore` 支持 vector index（`store/redis/aio.py:155-190`，需 `index_config`），
  但本项目**不使用**向量能力（`runtime/store/async_provider.py:91-94` 只传 `conn_string`，
  Postgres 的 `VECTOR_MIGRATIONS` 从未执行）—— 所以这不是 Redis 的加分项。
- `_batch_put_ops` 先 `FT.SEARCH` 找同 (prefix,key) 的旧文档再删（`store/redis/aio.py:627-660`），
  即每次 `aput` 都是一次搜索 + 一次删除 + 一次写入，写放大明显高于 Postgres 的单条 `ON CONFLICT`。

---

## 6. 运维与语义差异（最容易出事的一章）

这一章的内容**不会报错**，只会坏数据或丢数据。

| # | 差异 | Postgres（现状） | Redis | 风险 |
| --- | --- | --- | --- | --- |
| 1 | **淘汰策略** | 不存在"自动删行" | `maxmemory-policy` 非 `noeviction` 时**静默删除键**；`volatile-*` 优先删带 TTL 的键 | 🔴 **致命**。checkpoint 消失且应用层无感知 |
| 2 | **耐久性 / RPO** | WAL + fsync | compose 现状 `appendonly yes`（默认 `everysec`）→ 崩溃最多丢 ~1s 写入 | 🟠 会话状态可丢。要 RPO=0 须 `appendfsync always`，吞吐代价高 |
| 3 | **容量模型** | 磁盘为主，内存只放热数据 | **全部驻留内存**，无溢出 | 🟠 内存成本随 checkpoint 总量线性增长；`checkpoint_cache` 若同实例叠加 |
| 4 | **TTL 语义** | 无"自动过期"概念 | `ttl` 参数一旦设 `default_ttl`（分钟），checkpoint **自动消失**（`base.py:354-397`） | 🟠 默认 `None`（不过期），但一旦被打开就是静默数据丢失 |
| 5 | **命名空间隔离** | `database.postgres_schema` + `search_path` | `checkpoint_prefix` / `checkpoint_write_prefix`（`aio.py:88-103`） | 🟡 多部署共实例靠 prefix，不靠 schema。需同步改造 `checkpoint_cache_db_hash`（`checkpoint_cache/provider.py:44-53`，目前只认 postgres/sqlite/memory） |
| 6 | **Cluster** | 单实例 | 键名**无 `{...}` hash tag**（`__init__.py:145-172`），部分路径用 `pipeline(transaction=True)` | 🟡 Redis Cluster 下跨 slot 事务会失败。且 RediSearch 只在 Enterprise / Redis 8 cluster 可用 |
| 7 | **备份** | `pg_dump` / PITR 成熟 | RDB/AOF 文件级，一致性备份需额外方案 | 🟡 |
| 8 | **二进制体积** | `BYTEA` 原样 | base64（`base.py:626-630`） | 🟡 +33% |

**第 1 条是决定性的。** 一个"必须 `noeviction` 否则会静默丢数据"的配置契约，
和一个"默认就是 `noeviction` 但运维手册通常教人设 LRU"的生态，两者不匹配。

---

## 7. 为什么 Application Data 不受影响

`database.backend` 当前**同时**驱动 checkpointer/store 与 ORM（`config/database_config.py` 模块 docstring 明写
"Controls BOTH the LangGraph checkpointer and the DeerFlow application persistence layer"）。

应用层数据规模（`persistence/migrations/versions/`）：**23 个迁移**，
baseline 单文件即建 9 张表（`0001_baseline.py:52-223`），累计约 26 张关系表。

并且多 worker 有一条硬门禁（`app/gateway/deps.py:49-79`）：

```python
def _enforce_postgres_for_multi_worker(config: AppConfig) -> None:
    """... 2. The DB backend must be Postgres — SQLite write-locks cannot support
       concurrent multi-process access. ..."""
```

**推论：即使 checkpoint/store 迁到 Redis，应用层仍必须 Postgres，多 worker 门禁不能放松。**
"用 Redis 省掉一个 Postgres"这个动机**不成立**。

### 7.1 由此引出的配置模型问题（必须正面处理）

当前 `database.backend: Literal["memory","sqlite","postgres"]` 是**一个字段管两件事**
（`config/database_config.py:143`）。引入 Redis checkpointer 后会出现
**"ORM 在 Postgres、checkpoint 在 Redis"** 这种组合，现有模型无法表达。

两条路：

1. **拆字段**：`database.backend`（ORM）+ 新增 `database.checkpointer_backend`。
   语义清晰，但 `_resolve_checkpointer_config` / `_resolve_store_config`（两处同构函数）
   与所有读 `database.backend` 的代码都要跟着改。
2. **走 legacy `checkpointer:` 段**：`CheckpointerConfig.type` 加 `redis`（`config/checkpointer_config.py:9`）。
   改动面小，但会复活"legacy 段优先"这条本就计划退役的路径
   （`runtime/checkpointer/provider.py:58-79` 已标注 legacy）。

**推荐 1**，因为它把"应用数据后端"与"checkpoint 后端"的分歧**显式化**，
而不是靠一个 deprecated 段隐式覆盖。

---

## 8. 改造面清单

按"必须改"与"必须验证"分开。

### 8.1 必须改

| # | 位置 | 改动 |
| --- | --- | --- |
| 1 | `config/checkpointer_config.py:9` / `config/database_config.py:143` | 后端枚举加 `redis`；按 §7.1 决定是拆字段还是走 legacy 段 |
| 2 | `runtime/checkpointer/provider.py:111-149` | sync 分支加 redis |
| 3 | `runtime/checkpointer/async_provider.py:109-142` | async 分支加 redis（`AsyncRedisSaver` + `asetup()` + `aclose` 生命周期） |
| 4 | `runtime/store/provider.py:99-145`、`store/async_provider.py:51-97` | Store 两路分支 |
| 5 | `runtime/checkpointer/provider.py:58-79`、`store/provider.py:54-75` | 两处同构的 `_resolve_*_config` 加 redis 分支 |
| 6 | `app/gateway/health.py:218-237` | 探针白名单目前是 `("sqlite","postgres")`，未知后端 → `DATABASE_UNREACHABLE`。加 redis 分支（`PING` + 校验索引存在） |
| 7 | `runtime/checkpoint_cache/provider.py:44-53` | `checkpoint_cache_db_hash` 只认 postgres/sqlite/memory，加 redis 分支（否则缓存命名空间塌缩到 `"memory"`） |
| 8 | `docker/docker-compose.yaml:29-36`、`docker-compose-dev.yaml:40-47` | 换镜像（redis-stack / redis:8）+ `maxmemory-policy noeviction` + AOF 策略 |
| 9 | `deploy/helm/deer-flow/` | 同步镜像与参数（chart 里 redis 默认 enabled） |
| 10 | `packages/harness/pyproject.toml` | 新增 extra（如 `redis-checkpoint`），把 `langgraph-checkpoint-redis` + `redisvl` 关在里面 |

### 8.2 必须验证（不是"顺便测一下"）

| # | 项 | 为什么 |
| --- | --- | --- |
| V1 | **`alist(before=...)` 语义** | §4.5。必须写一个"翻页不重复、不遗漏"的用例；修好之前不能上生产 |
| V2 | **`limit=None` 的 10000 上限** | §4.7。长会话可能撞上，且静默 |
| V3 | **`asearch(filter=...)` + offset 分页** | §5.3。用 >500 条 thread 的账号验证线程列表完整 |
| V4 | **sync 路径（TUI / CLI）** | `AsyncRedisSaver` 的 sync 方法靠 `run_coroutine_threadsafe` 派发到 `asetup()` 捕获的 loop（`aio.py:248-256`）。TUI 没有长驻 loop，这条路必须单独跑通 |
| V5 | **delta 模式** | `aget_delta_channel_history` 未覆写（§4.4），走基类逐祖先遍历。要在真实长会话上量一下延迟 |
| V6 | **`CachedHistorySaver` 与 Redis 后端叠加** | 同步路径已明确拒绝 redis 缓存后端（`provider.py:181-182`）。异步路径若同时用 redis checkpointer + redis cache，需确认没有 key 前缀冲突 |
| V7 | **Redis 重启 / 主从切换期间的写入** | 现状 stream_bridge/cache 是"失败降级"；checkpoint 必须**失败即报错**，不能静默降级 |

### 8.3 不需要改（但要说明）

- `_enforce_postgres_for_multi_worker`（`deps.py:49-`）：**保持原样**，见 §7。
- `checkpoint_patches.py`：补丁打在 `InMemorySaver` 与 `BinaryOperatorAggregate` 上，
  与后端无关。
- `runtime/checkpoint_mode.py`：模式冻结与 metadata 标记是后端无关的逻辑。
- `CachedHistorySaver`：`self.serde = inner.serde`，契约一致，可原样复用。

---

## 9. 结论

### 9.1 判定

| 职责域 | 判定 | 代码证据 |
| --- | --- | --- |
| Checkpoint | **Feasible with significant changes** | 官方无实现（`.venv/…/langgraph/checkpoint/` 仅 base/memory/postgres/serde/sqlite）；第三方需 RedisJSON+RediSearch（METADATA:62-79）；`before` 游标静默失效（`aio.py:670-682` vs `postgres/base.py:591-594`）；打破单一后端模型（`database_config.py:143`） |
| Store | **Feasible with significant changes** | 第三方 `langgraph.store.redis`；索引不覆盖业务 metadata（`store/redis/aio.py:115-140`）；filter 后置导致分页不完整（`store/redis/base.py:575-598` + `aio.py:924-925`）；使用面极窄（`deps.py:498` + `thread_meta/__init__.py:41-45`） |
| **整体（作为生产默认）** | **Not currently recommended** | 见 §9.2 |

### 9.2 为什么整体不推荐

1. **收益不成立。** 最大的动机"少一个数据库"被 §7 否掉 —— 应用层与多 worker 仍需 Postgres。
   Store 的使用面又窄到近乎为零（§5.2）。真正被替换的只有 checkpoint 一个职责。
2. **代价是换基础设施。** 现有 `redis:7-alpine` 不满足模块要求（§4.3），
   要换 Redis Stack / Redis 8 / 云企业版 —— 这不是配置改动。
3. **把"唯一真相"放进会静默淘汰数据的内存库。** 项目现有两处 Redis 用法
   （stream_bridge、checkpoint_cache）都建立在"Redis 挂了不影响正确性"上
   （`checkpoint_cache/redis.py:69` 的注释就是这句）。checkpoint 没有这个性质（§6 第 1 条）。
4. **第三方实现带两个已证实的静默缺陷。** `before` 游标（§4.5）与 store 分页过滤（§5.3）
   都会"不报错地返回错的结果"。采用它等于接受维护一个本地补丁。
5. **没有平滑回滚。** 物理表示不同构（§4.8），回滚同样要一次 API 级重写。

### 9.3 什么条件下结论会变化

| 条件 | 结论变化 |
| --- | --- |
| 目标实例已是 Redis Stack / Redis 8 / 云企业版，且 `maxmemory-policy noeviction` 是既有标准 | 阻碍从"换基础设施"降到"改代码"，Checkpoint 仍为 **Feasible with significant changes** |
| 组织愿意 fork / 上游接受 `before` 游标修复 | 可去掉第 4 条的一半理由 |
| 产品需求变成"checkpoint 必须与 Postgres 解耦"（例如把 Postgres 收敛成纯 ORM 库） | 动机成立，整体判定可升到 **Feasible with significant changes** |
| 要求 RPO=0 且不接受 `appendfsync always` 的吞吐代价 | 退回 **Not currently recommended** |

---

## 10. 推荐路径

### 10.1 短期（零新依赖，立即可做）

**不要动 checkpoint/store 后端。** 若要缓解 checkpoint 的写放大与读延迟，
用项目**已经支持**的两条现成路径：

```yaml
database:
  backend: postgres
  checkpoint_channel_mode: delta          # 消息通道走 DeltaChannel
  checkpoint_cache:
    type: redis                           # 多 worker 共享历史缓存
    redis_url: $DEER_FLOW_CHECKPOINT_CACHE_REDIS_URL
    ttl_seconds: 86400
```

`checkpoint_cache.type: redis` 已经是完整实现（`runtime/checkpoint_cache/redis.py`），
且设计上就是"Redis 故障只损失命中率"（`redis.py:68-72`）。这是**风险与收益比最好的一步**。

### 10.2 若一定要上 Redis checkpointer

按顺序做，不要并行：

1. **先修 `before` 游标。** 给 `langgraph-checkpoint-redis` 提 issue，同时准备本地补丁
   （把 `ULID.from_str` 换成"ULID 解析失败则退化为 `checkpoint_id <` 字典序比较"，
   对齐 Postgres 语义）。**在 V1 用例通过前不要往下走。**
2. **再拆配置模型**（§7.1 方案 1），让 `database.checkpointer_backend` 独立于 `database.backend`。
3. **最后接后端**，并把 V1–V7 全部补齐为 `integration` 标记用例
   （`backend/pyproject.toml:71` 已有该 marker，Redis 不可用时按设计跳过 —— 不会污染 CI）。
4. **同步更新运维契约**：`maxmemory-policy noeviction`、AOF 策略与 RPO、
   容量规划（含 base64 的 +33%）、备份方案。

### 10.3 明确不做的事

- **不要**为了 Redis checkpointer 放松 `_enforce_postgres_for_multi_worker`。
- **不要**把 store 也迁到 Redis —— 使用面太窄，收益为零，却要背 §5.3 的分页缺陷。
- **不要**给 checkpointer 配 `ttl` —— 那是把"永久"改成"会自动消失"，且没有告警。

---

## 附录 A：审计方法

按 skill `db-backend-migration-audit` 的只读流程：

1. **锁定真实版本** —— `ls .venv/…/site-packages/`，不信 `pyproject.toml` 声明。
2. **证明官方支持面** —— `ls .venv/…/langgraph/checkpoint/` 一次定论，不翻文档。
3. **审计第三方实现** —— `curl` 取 PyPI JSON → 下载 wheel → `unzip` 到 `/tmp` →
   读源码与 `MIGRATIONS`/索引 schema。**不安装、不写入仓库。**
4. **盘点项目自身原语** —— 按语义分类（API 契约 / 配置模型 / 健康探针 / 部署），
   不只搜字符串 `redis`。
5. **判断是否真的需要迁** —— 先问"被迁的组件真的在用目标后端吗"（§5.2 的答案是"几乎没用"）。
6. **找隐性约束** —— §6，跨后端不报错只坏数据的那一类。
7. **落到四个档位之一并给代码证据。**

实证脚本与解包产物位于 `/tmp/redis-audit/`（临时目录，未进入仓库）：

```
/tmp/redis-audit/src/        langgraph_checkpoint_redis-0.5.2 解包
/tmp/redis-audit/pulidsrc/   python-ulid-4.0.1 解包（用于验证 §4.5）
```

## 附录 B：环境坑（本机）

- Bash 的 `grep` / `find` / `sed` / `tail` 是 WorkBuddy 的 toybox 垫片，不是 GNU 版本。
  代码搜索一律用 Grep 工具（ripgrep）；shell 脚本里写绝对路径 `/usr/bin/sed` 等。
- 隔离 venv 里 `pip install` 可能被沙箱阻断 → 只做 `pip download` / `curl` 解包阅读。
- 读 `.venv` 里的包要用 `./.venv/bin/python`（系统 `python3` 缺 `langchain_core` 的依赖链）。
