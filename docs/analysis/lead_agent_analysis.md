# DeerFlow Lead Agent 分析

## 1. 实现方式

Lead Agent 基于 **LangGraph** 的 `create_agent` 工厂函数构建，入口函数为 `make_lead_agent(config: RunnableConfig)`。

**核心组件**：
- `agent.py`: `make_lead_agent()` — 工厂函数，创建 agent 实例
- `prompt.py`: `apply_prompt_template()` — 动态生成系统提示词

---

## 2. 主要功能

| 功能 | 说明 |
|------|------|
| **任务编排** | 作为主 agent 协调所有工具和子 agent |
| **动态模型选择** | 支持 thinking/vision，基于配置解析模型 |
| **子 agent 委托** | 通过 `task` 工具将复杂任务委托给 subagent |
| **工具调用** | 组合 sandbox tools、built-in tools、MCP tools、community tools |
| **记忆系统** | 通过 MemoryMiddleware 队列化对话用于记忆更新 |
| **上下文摘要** | SummarizationMiddleware 在 token 接近限制时压缩上下文 |
| **标题生成** | TitleMiddleware 在首次完整对话后自动生成 thread title |
| **Todo 追踪** | Plan mode 下启用 TodoMiddleware 提供 `write_todos` 工具 |
| **循环检测** | LoopDetectionMiddleware 防止重复工具调用循环 |
| **图片查看** | ViewImageMiddleware 将图片转为 base64 注入给有 vision 能力的模型 |
| **澄清拦截** | ClarificationMiddleware 拦截 `ask_clarification` 调用并中断执行 |

---

## 3. 参数列表

**`make_lead_agent(config: RunnableConfig)`** 从 `config.configurable` 读取：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `thinking_enabled` | bool | True | 是否启用 extended thinking |
| `reasoning_effort` | str | None | 推理强度（模型特定） |
| `model_name` / `model` | str | None | 指定模型名（优先级高于 agent 配置） |
| `is_plan_mode` | bool | False | 是否启用 TodoList 中间件 |
| `subagent_enabled` | bool | False | 是否启用 task 委托工具 |
| `max_concurrent_subagents` | int | 3 | 单次响应最大 `task` 调用数 |
| `is_bootstrap` | bool | False | 引导模式（最小化的引导 agent） |
| `agent_name` | str | None | 加载对应的 agent soul 和技能配置 |

---

## 4. 设计逻辑

```
用户请求
    ↓
make_lead_agent()
    ├── 解析 runtime config
    ├── _resolve_model_name() → 优先级: request > agent_config > default
    ├── get_available_tools() → 收集所有可用工具
    ├── _build_middlewares() → 构建 17 个中间件链
    └── apply_prompt_template() → 动态组装系统提示词
            ├── Soul (agent 性格)
            ├── Memory context
            ├── Skills section
            ├── Subagent section (if enabled)
            ├── Deferred tools section
            └── Working directory
    ↓
create_agent(model, tools, middleware, system_prompt, state_schema)
```

### 中间件执行顺序

Lead-agent 中间件在 `build_lead_runtime_middlewares` + `_build_middlewares` 中按严格顺序组装：

| # | 中间件 | 说明 |
|---|--------|------|
| 1 | `ThreadDataMiddleware` | 创建 per-thread 目录结构 |
| 2 | `UploadsMiddleware` | 注入上传文件列表 |
| 3 | `SandboxMiddleware` | 获取沙箱 |
| 4 | `DanglingToolCallMiddleware` | 修补中断的 tool_calls |
| 5 | `LLMErrorHandlingMiddleware` | 规范化模型错误 |
| 6 | `GuardrailMiddleware` | 工具调用授权（可选） |
| 7 | `SandboxAuditMiddleware` | 安全审计 |
| 8 | `ToolErrorHandlingMiddleware` | 工具异常处理 |
| 9 | `SummarizationMiddleware` | 上下文压缩（可选） |
| 10 | `TodoListMiddleware` | 任务追踪（可选） |
| 11 | `TokenUsageMiddleware` | token 用量统计（可选） |
| 12 | `TitleMiddleware` | 生成 thread title |
| 13 | `MemoryMiddleware` | 记忆队列化 |
| 14 | `ViewImageMiddleware` | 图片注入（有 vision 模型） |
| 15 | `DeferredToolFilterMiddleware` | 隐藏延迟工具（tool_search 启用） |
| 16 | `SubagentLimitMiddleware` | 截断超额 task 调用 |
| 17 | `LoopDetectionMiddleware` | 循环检测 |
| 18 | `ClarificationMiddleware` | 澄清拦截（最后） |

---

## 5. 与 Subagent 的协作方式

### 5.1 启用与配置

Subagent 通过 `subagent_enabled=true` 启用，lead agent 会获得 `task` 工具。

### 5.2 委托流程

```
Lead Agent                              SubagentExecutor
    │                                        │
    │  task(description, prompt, type)       │
    │────────────────────────────────────────►
    │                                        │
    │   创建 SubagentExecutor                │
    │   • 继承 parent model                  │
    │   • 过滤工具 (allowed/禁用)            │
    │   • 传递 sandbox_state/thread_data     │
    │                                        │
    │   execute_async() → task_id            │
    │                                        │
    │              poll 5s → 结果            │
    │◄────────────────────────────────────────
    │                                        │
    │  合并 subagent 结果到响应               │
```

### 5.3 并发控制

- **SubagentLimitMiddleware**: 硬性限制单次响应最多 `MAX_CONCURRENT_SUBAGENTS=3` 个 `task` 调用
- **超过限制时**: 多出的 task 调用被静默丢弃
- **多批次执行**: `>3` 个子任务需要分批，每批 ≤3

### 5.4 通信事件

| 事件 | 说明 |
|------|------|
| `task_started` | subagent 开始执行 |
| `task_running` | 仍在运行 |
| `task_completed` | 成功完成，返回 result |
| `task_failed` | 执行失败，返回 error |
| `task_timed_out` | 超时（默认 15 分钟） |

### 5.5 取消机制

`request_cancel_background_task(task_id)` 设置 `cancel_event`，subagent 在 `astream` 迭代边界检查并优雅停止。

---

## 6. ThreadState 数据结构

```python
class ThreadState(AgentState):
    messages: list[BaseMessage]         # 对话历史
    sandbox: SandboxState               # {sandbox_id}
    thread_data: ThreadDataState        # {workspace_path, uploads_path, outputs_path}
    title: str                          # thread 标题
    artifacts: list[str]                 # 工件文件路径（去重归并）
    todos: list                          # TodoList 任务
    uploaded_files: list[dict]           # 上传文件元数据
    viewed_images: dict[str, ViewedImageData]  # base64 图片数据
```

---

## 7. 关键设计亮点

1. **lazy init 中间件**: `_build_middlewares` 大部分中间件使用 `lazy_init=True`，延迟初始化避免循环依赖
2. **三级线程池**: scheduler(3) + execution(3) + isolated_loop(3)，隔离同步/异步调用
3. **token 预算感知**: SummarizationMiddleware 在接近限制时触发上下文压缩
4. **多批次任务**: 通过 middleware 强制批次化，避免一次性托付过多 subagent

---

## 8. is_bootstrap 的作用

`is_bootstrap` 用于创建一个**最小化的引导 Agent**，专门用于初始的自定义 agent 创建流程。

```python
if is_bootstrap:
    return create_agent(
        model=create_chat_model(name=model_name, thinking_enabled=thinking_enabled),
        tools=get_available_tools(...) + [setup_agent],
        middleware=_build_middlewares(...),
        system_prompt=apply_prompt_template(
            subagent_enabled=subagent_enabled,
            max_concurrent_subagents=max_concurrent_subagents,
            available_skills=set(["bootstrap"])  # 只加载 bootstrap 技能
        ),
        state_schema=ThreadState,
    )
```

**关键差异**：

| 特性 | 普通 Lead Agent | Bootstrap Agent |
|------|----------------|-----------------|
| `available_skills` | 从 `agent_config.skills` 加载所有技能 | 仅 `{"bootstrap"}` 一个技能 |
| 工具 | `get_available_tools(groups=agent_config.tool_groups)` | 额外包含 `setup_agent` |
| 用途 | 通用任务执行 | 引导用户创建自定义 agent |

**bootstrap 场景**：当用户首次使用或需要创建新的 agent 配置时，系统会用这个最小化的 bootstrap agent，它只暴露 `bootstrap` 技能和 `setup_agent` 工具，避免过早加载完整功能，让用户逐步配置自己的 agent。