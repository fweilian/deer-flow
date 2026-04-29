# DeerFlow Runtime 模块分析

`runtime/` 位于 `backend/packages/harness/deerflow/runtime/`，是 **Gateway 模式**（嵌入式 Agent 运行时）的核心组件，替代了独立 LangGraph Server 的部分功能。

## 整体架构

```
runtime/
├── runs/           # Run 生命周期管理 + 后台 Agent 执行
├── store/          # LangGraph Store (内存/SQLite/PostgreSQL)
├── stream_bridge/  # 生产者-消费者之间的流式事件解耦
├── serialization.py # LangChain/LangGraph 对象的序列化工具
└── __init__.py     # 公共 API 导出
```

---

## 1. runs/ — Run 生命周期管理

### runs/schemas.py — 枚举定义

- **`RunStatus`**：run 的生命周期状态
  - `pending` → `running` → `success` / `error` / `timeout` / `interrupted`
- **`DisconnectMode`**：SSE 消费者断开时的行为
  - `cancel`：取消 run
  - `continue_`：继续执行

### runs/manager.py — `RunManager`

内存中的 run 注册表，负责：
- **`create()`** / **`create_or_reject()`**：创建新 run
- **`get()`** / **`list_by_thread()`**：查询 run
- **`set_status()`**：更新状态
- **`cancel()`**：请求取消（支持 `interrupt` 和 `rollback` 两种策略）
- **`cleanup()`**：延迟删除 run 记录
- **`has_inflight()`**：检查线程是否有进行中的 run

**多任务策略** (`multitask_strategy`)：
- `reject`：线程已有运行中 run 时抛出 `ConflictError`
- `interrupt`/`rollback`：中断已有 run 后创建新 run

### runs/worker.py — `run_agent()`

后台 asyncio Task，负责：
1. 调用 `graph.astream()` 执行 Agent
2. 将 LangGraph 事件发布到 `StreamBridge`
3. 支持多种 `stream_mode`：`values`、`updates`、`messages`、`custom` 等
4. 捕获 `abort_event` 实现取消/回滚
5. 执行前快照 checkpoint，以便 rollback 时恢复

---

## 2. store/ — LangGraph Store 工厂

Store 与 Checkpointer 共用同一份 `config.yaml` 配置：

| 配置类型 | Store 实现 | 说明 |
|---------|-----------|------|
| `memory` | `InMemoryStore` | 进程内，非持久化 |
| `sqlite` | `AsyncSqliteStore` | SQLite 持久化 |
| `postgres` | `AsyncPostgresStore` | PostgreSQL 持久化 |

**导出 API**：
- **`make_store()`** — 异步上下文管理器（FastAPI 服务用）
- **`get_store()`** — 同步单例（CLI / `DeerFlowClient` 用）
- **`store_context()`** — 同步上下文管理器（一次性使用）
- **`reset_store()`** — 重置单例

---

## 3. stream_bridge/ — 生产者-消费者解耦

### stream_bridge/base.py

```python
class StreamBridge(ABC):
    async def publish(run_id, event, data)    # 生产者：发布事件
    async def publish_end(run_id)              # 生产者：结束信号
    def subscribe(run_id, last_event_id, ...)  # 消费者：订阅事件流
    async def cleanup(run_id, delay)          # 释放资源
```

**`StreamEvent`** 数据类：
- `id`：单调递增，用于 `Last-Event-ID` 断线重连
- `event`：SSE 事件名（如 `metadata`、`values`、`messages`、`error`、`end`）
- `data`：JSON 可序列化的负载

**两个哨兵事件**：
- `HEARTBEAT_SENTINEL`：心跳（15s 无事件时发出）
- `END_SENTINEL`：流结束

### stream_bridge/memory.py — `MemoryStreamBridge`

内存版实现：
- 每个 run 维护一个 `list[StreamEvent]` + `asyncio.Condition`
- 支持 `Last-Event-ID` 断线重连（从保留窗口内查找）
- 缓冲区大小可配置（默认 256 条）
- 超出窗口的旧事件被丢弃

### stream_bridge/async_provider.py — `make_stream_bridge()`

异步上下文管理器工厂，当前仅支持 `memory` 类型（`redis` 为 Phase 2 规划）。

### make_stream_bridge() 的作用

它是一个异步上下文管理器工厂，用于在 FastAPI 服务启动/关闭时统一管理 `StreamBridge` 的生命周期。

**核心职责**：

```python
async with make_stream_bridge() as bridge:
    app.state.stream_bridge = bridge  # 启动时创建
# ← 退出时自动调用 bridge.close() 清理资源
```

**解决的问题**：
1. **资源生命周期管理**：确保 `MemoryStreamBridge` 在服务关闭时被正确关闭
2. **配置注入**：从 `config.yaml` 读取 `stream_bridge` 配置
3. **解耦生产与消费**：
   - `run_agent()`（后台 Task）调用 `bridge.publish()` 生产事件
   - SSE endpoint 调用 `bridge.subscribe()` 消费事件
   - 两者通过 `run_id` 关联，不直接依赖彼此

---

## 4. serialization.py — LangChain 对象序列化

统一将 LangChain/Pydantic 对象转为 JSON 可序列化格式：

| 函数 | 用途 |
|-----|------|
| `serialize_lc_object()` | 递归序列化（支持 Pydantic v1/v2、dict、list 等） |
| `serialize_channel_values()` | 序列化 state，剥离 `__pregel_*` 和 `__interrupt__` 内部 key |
| `serialize_messages_tuple()` | 序列化 `(chunk, metadata)` 元组 |
| `serialize()` | 主入口，按 mode 分发（`messages` / `values` / 默认） |

消费者：`runs/worker.py`（SSE 发布）和 `app.gateway.routers.threads`（REST 响应）。

---

## 使用方式

### Gateway 模式启动时（FastAPI lifespan）

```python
from deerflow.runtime import make_store, make_stream_bridge

async with make_store() as store:
    app.state.store = store

async with make_stream_bridge() as bridge:
    app.state.stream_bridge = bridge
```

### 创建 run 并执行 agent

```python
from deerflow.runtime import RunManager, run_agent

run_manager = RunManager()
record = await run_manager.create_or_reject(thread_id, multitask_strategy="interrupt")

# 在后台 asyncio.Task 中运行 agent
task = asyncio.create_task(run_agent(
    bridge=bridge,
    run_manager=run_manager,
    record=record,
    checkpointer=checkpointer,
    store=store,
    agent_factory=make_lead_agent,
    graph_input={"messages": [...]},
    config={...},
))
```

### SSE 消费端（Gateway 路由）

```python
async for event in bridge.subscribe(run_id, last_event_id="..."):
    if event is END_SENTINEL:
        break
    if event is HEARTBEAT_SENTINEL:
        continue
    # 发送 SSE 到客户端
    await sse_event(event.id, event.event, event.data)
```

---

## 设计目标

- **解耦**：StreamBridge 将 agent worker（生产者）与 SSE 端点（消费者）解耦，类似 LangGraph Platform 的 Queue + StreamManager
- **LangGraph 兼容**：`run_agent()` 适配 LangGraph 的 `astream(stream_mode=[...])` API
- **多模式支持**：values、updates、messages、custom 等多种流模式
- **回滚支持**：通过 pre-run checkpoint snapshot 实现 abort 时的状态回滚
- **持久化**：Store 与 Checkpointer 共用配置，支持内存/SQLite/PostgreSQL 后端