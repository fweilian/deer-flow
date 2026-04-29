# Deer-Flow Sandbox 架构分析

---

### 一、整体架构概览

Deer-Flow 采用**抽象接口 + 多实现**的架构模式，核心接口定义在 `sandbox/`，Docker 容器化实现在 `community/aio_sandbox/`。

```
deerflow.sandbox          # 核心抽象层
├── sandbox.py            # Sandbox 抽象基类
├── sandbox_provider.py  # SandboxProvider 生命周期管理 + 单例模式
├── tools.py             # 工具实现 (bash/ls/glob/grep/read_file/write_file)
├── middleware.py        # LangGraph AgentMiddleware
└── local/               # 本地执行实现 (直接调 host)

deerflow.community.aio_sandbox  # Docker 容器化实现
├── aio_sandbox_provider.py   # 生命周期编排 + 暖池管理
├── aio_sandbox.py            # HTTP 客户端 (容器内 API)
├── backend.py                # 容器后端抽象
├── local_backend.py          # Docker/Apple Container 本地管理
└── remote_backend.py          # 远程 k3s Pod 委托
```

---

### 二、核心接口设计

#### 2.1 `Sandbox` 抽象基类 (`sandbox/sandbox.py`)

所有沙箱实现必须实现的接口：

| 方法 | 作用 |
|------|------|
| `execute_command(command)` | 执行 bash 命令，返回 stdout |
| `read_file(path)` | 读取文件内容 |
| `list_dir(path, max_depth=2)` | 目录树列表 |
| `write_file(path, content, append=False)` | 写入文件 |
| `glob(pattern, ...)` | 文件模式匹配 |
| `grep(pattern, ...)` | 正则搜索 |
| `update_file(path, content)` | 全量更新文件内容 |

#### 2.2 `SandboxProvider` 抽象基类 (`sandbox/sandbox_provider.py`)

沙箱的**生命周期管理者**，实现者需实现：

| 方法 | 作用 |
|------|------|
| `acquire(thread_id)` | 获取沙箱 ID（线程安全） |
| `get(sandbox_id)` | 根据 ID 获取 Sandbox 实例 |
| `release(sandbox_id)` | 释放沙箱（可能放入暖池） |

Provider 支持单例管理，通过 `get_sandbox_provider()` 获取，`set_sandbox_provider()` 可注入自定义/mock 实现。

---

### 三、工具层实现逻辑 (`sandbox/tools.py`)

这是 Agent 调用工具的入口层。

#### 3.1 核心设计模式

- **懒初始化**：`ensure_sandbox_initialized(runtime)` 在首次工具调用时才从 provider 申请沙箱
- **虚拟路径映射**：Agent 看到的路径是 `/mnt/user-data/workspace` 等，实际 host 上可能是另一路径
- **路径双向转换**：
  - Agent → Host：`replace_virtual_paths_in_command()` + `_resolve_path()`
  - Host → Agent：`mask_local_paths_in_output()` — 输出时反向转换回虚拟路径

#### 3.2 路径体系

| 虚拟路径 | 说明 |
|----------|------|
| `/mnt/user-data/workspace` | 工作目录 |
| `/mnt/user-data/uploads` | 上传文件目录 |
| `/mnt/user-data/outputs` | 输出文件目录 |
| `/mnt/skills` | Skills（只读） |
| `/mnt/acp-workspace` | ACP 工作区（Agent 只读，ACP 子进程可写） |

#### 3.3 重要函数实现逻辑

**`bash_tool`**
```
1. 检查 is_host_bash_allowed()（安全门控）
2. replace_virtual_paths_in_command() — 将 /mnt/* 映射到 host 实际路径
3. 追加 CWD 前缀防止无处执行
4. subprocess 执行，捕获 stdout/stderr
5. mask_local_paths_in_output() 反向转换输出中的路径
6. 按配置截断超长输出
```

**`str_replace_tool`（原子性文件修改）**
```
1. 获取 file_operation_lock — 防止并发读写同一文件
2. 读取文件内容
3. 定位 old_str（精确匹配）
4. 替换为 new_str（支持 replace_all）
5. 写回文件
6. 释放锁
```

**`glob_tool` / `grep_tool`**
- 调用 `find_glob_matches()` / `find_grep_matches()`（在 `search.py`）
- `GrepMatch` 包含 `path, line_number, line_content`
- 结果限制 `max_results`，`truncated` 标志表示被截断

---

### 四、LocalSandbox 实现 (`sandbox/local/local_sandbox.py`)

本地开发/调试模式，命令直接在 host 执行。

#### 4.1 路径映射机制

```python
@dataclass
class PathMapping:
    virtual_path: str       # /mnt/user-data/workspace
    actual_path: str        # /home/user/.deerflow/thread_data/xxx/workspace
    read_only: bool        # 只读标记
```

`_resolve_path()`: 虚拟 → 实际
`_reverse_resolve_path()`: 实际 → 虚拟

关键设计：`_agent_written_paths` 集合只记录 agent 通过 `write_file` 写入的文件，避免对用户上传内容的误反向映射。

#### 4.2 Shell 检测

尝试 `/bin/zsh` → `/bin/bash` → `sh`（macOS/Linux），Windows 上用 `powershell` → `cmd`。

---

### 五、AioSandboxProvider 架构 (`community/aio_sandbox/aio_sandbox_provider.py`)

生产级 Docker 容器编排，是最复杂的模块。

#### 5.1 三层一致性模型

```
Layer 1: 进程内缓存  (_sandboxes, _thread_sandboxes)
Layer 1.5: 暖池      (_warm_pool) — 已释放但仍运行的容器
Layer 2: 跨进程发现  (文件系统锁 + Docker 容器名)
```

#### 5.2 Deterministic Sandbox ID

`sandbox_id = sha256(thread_id)[:8]`

同一 `thread_id` 在任何进程中都映射到同一个容器名，实现跨进程发现。

#### 5.3 跨进程锁

文件锁位于 `{thread_dir}/{sandbox_id}.lock`，确保多进程不会并发创建同一个容器。

#### 5.4 暖池与回收

- `release(sandbox_id)` → 移入暖池（容器保持运行）
- 后台线程每 60s 检查一次空闲超时
- `idle_timeout=600`（10分钟），超时则真正 `destroy()`
- `replicas=3` 软限制（可突破）

#### 5.5 启动时协调

进程启动时扫描所有已存在的同名容器，纳入暖池管理（支持进程重启后恢复）。

#### 5.6 信号处理

SIGTERM/SIGINT/SIGHUP 统一触发 `shutdown()` 优雅退出。

---

### 六、Backend 子系统

```
SandboxBackend (抽象)
├── LocalContainerBackend  # Docker / Apple Container
└── RemoteSandboxBackend # 远程 k3s Pod
```

#### 6.1 LocalContainerBackend

- **macOS Apple Container 检测**：优先使用 Apple Container，fallback 到 Docker
- **端口分配**：`get_free_port()` 线程安全 + bind 失败重试
- **批量 inspect**：一次性 `docker inspect` 所有容器（避免 N+1）
- **Mount 格式**：Docker 用 `--mount type=bind`（避免 Windows 盘符 `:` 问题），Apple Container 用 `-v`

#### 6.2 RemoteSandboxBackend

薄 HTTP 客户端委托给 provisioner 服务：

| 操作 | HTTP 方法 |
|------|-----------|
| 创建 | POST /api/sandboxes |
| 发现/存活检查 | GET /api/sandboxes/{sandbox_id} |
| 销毁 | DELETE /api/sandboxes/{sandbox_id} |

#### 6.3 AioSandbox（HTTP 客户端）

- **线程锁序列化 exec_command**：AIO 容器是单持久 shell session，并发调用会 corrupt
- **ErrorObservation 重试**：检测到 corrupt 后用 fresh session ID 重试
- **所有操作共用同一锁**：execute_command / list_dir / write_file / update_file

---

### 七、Middleware 层 (`sandbox/middleware.py`)

`SandboxMiddleware` 继承 LangGraph 的 `AgentMiddleware`，管理沙箱生命周期：

- `lazy_init=True`（默认）：沙箱在首次 tool call 时才获取
- `lazy_init=False`：在 `before_agent()` 中预获取
- **跨 turn 复用**：沙箱不会在每个 agent call 后释放（由 Provider 的 `release()` 决定何时真正销毁）
- **进程级清理**：`SandboxProvider.shutdown()` 在应用退出时统一清理

---

### 八、扩展方式

#### 8.1 新增 Sandbox Provider

1. 实现 `SandboxProvider` 抽象基类（`acquire/get/release`）
2. 设置 `uses_thread_data_mounts = True/False`
3. 在 `config.yaml` 注册：

```yaml
sandbox:
  use: your.module:YourSandboxProvider
```

#### 8.2 新增 Backend（用于 AioSandboxProvider）

1. 继承 `SandboxBackend`
2. 实现 `create/destroy/is_alive/discover`（可选 `list_running`）
3. 修改 `AioSandboxProvider._create_backend()` 工厂方法选择

#### 8.3 新增工具

```python
@tool("your_tool", parse_docstring=True)
def your_tool(runtime, description, ...):
    sandbox = ensure_sandbox_initialized(runtime)
    # 工具逻辑
    return result
```

注意：
- 路径使用虚拟路径，内部自动转换
- 涉及文件写入时使用 `file_operation_lock`
- 输出过长时按配置截断

#### 8.4 新增路径家族

在 `tools.py` 的 `_thread_virtual_to_actual_mappings()` 扩展：

```python
def _thread_virtual_to_actual_mappings(thread_data):
    mappings = [...]
    # 添加新的路径映射
    return mappings
```

---

### 九、配置参考

```yaml
sandbox:
  use: deerflow.sandbox.local:LocalSandboxProvider  # 开发环境
  # use: deerflow.community.aio_sandbox:AioSandboxProvider  # 生产环境
  allow_host_bash: false

  # AioSandbox 特有
  image: enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:latest
  port: 8080
  container_prefix: deer-flow-sandbox
  idle_timeout: 600
  replicas: 3
  mounts:
    - host_path: /path/on/host
      container_path: /path/in/container
      read_only: false
```
