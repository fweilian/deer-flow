# Phase 0 运行态基线快照

> 对应 `feature-inventory.md` §16 **Phase 0.1 / 0.2**。
> 采集方式：全部为**只读取证**（文件哈希、进程端口、HTTP 探测、SQLite 只读连接）。未改代码、未改配置、未跑删除。

## 1. 采集元信息

| 项             | 值                                                                        |
| ------------- | ------------------------------------------------------------------------ |
| 采集时刻          | 2026-09-16 12:51 CST                                                     |
| 代码版本（SHA）     | `f7f4a022e6a95228430d794326c2f109287a31c6`                                |
| 分支            | `feat_portal`                                                            |
| 工作区状态         | 仅 `.workbuddy-ai/`、`docs/architecture/` 未跟踪（无已跟踪文件的本地修改）                    |
| 上游            | `origin/main` = `origin/feat_portal` = 同一提交                            |

> **为什么必须记 SHA**：`feature-inventory.md` 第 9 行只写了"时间基线 2026-09-16，仓库 HEAD 工作区状态"，**没有 commit 锚点**。而 `config.yaml` 与 `extensions_config.json` 都在 `.gitignore`（L36–38）里，**无法靠 git 恢复基线**。本文档是唯一的可追溯锚点。

## 2. 配置文件基线（0.1）

| 文件                        | 状态          | 大小 / 哈希                                                        |
| ------------------------- | ----------- | -------------------------------------------------------------- |
| `config.yaml`             | ✅ 存在        | 6442 B，MD5 `23d7ede2f3d773b8bc156d5aeb7bf0fd`，`config_version: 44` |
| `extensions_config.json`  | ❌ **不存在**   | —                                                              |
| `extensions_config.example.json` | ✅ 存在 | 2303 B（模板，非生效文件）                                               |

冻结副本存放于本目录：

- `config.yaml.frozen`（与源文件逐字节一致，已用 `cmp` 校验）
- `extensions_config.example.json.frozen`（同上，作为"模板原样"存档）

`config.yaml` 中的 `api_key` 全部是 `$ENV_VAR` 引用形式（如 `$DEEPSEEK_API_KEY`），**不含字面密钥**，可安全入库存档。

### 2.1 `extensions_config.json` 缺失的真实影响（关键）

不是"少个文件无所谓"，而是**当前所有 skill 处于全开状态**：

- `ExtensionsConfig.resolve_config_path()`（`config/extensions_config.py` L490–493）找不到文件时**返回 `None`**，`from_file()` L511–513 返回空配置 `cls(mcp_servers={}, skills={})`。这是**设计预期行为**（注释明写 "Extensions are optional"），不是报错。
- `ExtensionsConfig.is_skill_enabled()`（L567–586）：`self.skills` 里**没有条目 → 默认 `True`**（`public`/`custom`/`legacy`/`integrations` 四类皆然）。

→ **实测结论：`skills/public/` 下 23 个 skill 当前全部启用**，没有任何一个被 `extensions_config.json` 关掉。

**这直接卡住 Phase 1.3**（"在 `extensions_config.json:skills` 中禁用 17 个研究/内容生成/半通用 skill"）——**文件不存在，该步骤无从下手**。这是 Phase 0.1 必须先补的硬前置。

> ⚠️ **不要把 `extensions_config.example.json` 原样复制成生效文件**。它带着 `"mcpInterceptors": ["my_package.mcp.auth:build_auth_interceptor"]` 这个**示例用的假模块路径**，以及 5 个与本期无关的 MCP server（github / parallel-search / openviking / postgres / long-running-reports）。
> 好消息是它**不会导致启动失败**：拦截器加载包在 `try/except` 里（`mcp/interceptors.py` L58–75），失败只打一条 `Failed to load MCP interceptor ...` warning。但仍应写一份**最小干净**的生效文件（`{"middlewares": [], "mcpServers": {}, "skills": {...}}`），不要从 example 派生。

### 2.2 `config.yaml` 生效的关键开关（与 Phase 1 直接相关）

| 配置键                                     | 当前值       | Phase 1 目标                        |
| --------------------------------------- | --------- | --------------------------------- |
| `tools[].image_search`                  | **已启用**   | 1.1 移除                            |
| `tool_groups[]`                         | 含 `web` / `browser` / `knowledge` | 1.2 移除这三个，保留 `file:read` / `file:write` / `bash` |
| `scheduler.enabled`                     | `false`   | 1.4 改为 `true`                     |
| `agents_api.enabled`                    | `false`   | 1.7 改为 `true`                     |
| `task_continuity.enabled`               | `false`   | 1.6 标"待评估启用"（**不是噪音关闭**）           |
| `subagent_batches.enabled`              | `false`   | 1.6 同上                            |
| `database.backend`                      | `sqlite`  | 1.5 生产切 `postgres`                 |
| `run_events.backend`                    | `memory`  | 1.5 切 `db`（Rev.3/Q5：审计硬需求）         |
| `agent_storage.backend`                 | `file`    | 1.5 切 `db`                         |
| `stream_bridge`                         | **配置中无该键** → 走默认 `memory` | 1.5 切 `redis`                      |
| `channel_connections.enabled`           | `false`   | 1.4 保持关闭                          |
| `mcp_tasks.enabled`                     | `false`   | 1.4 保持关闭                          |
| `suggestions.enabled` / `input_polish.enabled` | 均 `true` | Phase 2.5 删除对应路由与配置节              |
| `skill_scan.enabled`                    | `true`    | Q20：保留                            |
| `skill_evolution.enabled`               | `false`   | 保持关闭                              |
| `authorization.enabled`                 | `false`   | Q3：AuthZ 自建替换，只留 Provider 协议缝     |

> 注：`stream_bridge` 在 `config.yaml` 中**没有显式键**，因此 `feature-inventory.md` §5.2 写的"默认 memory"是按代码默认值判定的，不是从配置文件读出来的。Phase 1.5 要切 `redis` 需要**新增**该键，而不是改一个已有值。

## 3. 运行态基线

### 3.1 服务（采集时刻均在监听）

| 服务      | 端口     | 进程                                   | 状态                              |
| ------- | ------ | ------------------------------------ | ------------------------------- |
| Nginx   | `2026` | `nginx` pid 21736 / 21738            | LISTEN v4+v6                    |
| Gateway | `8001` | `python3.1` pid 21193 / 21243（uvicorn reload） | LISTEN v4                       |
| Frontend | `3000` | `node` pid 21697                      | LISTEN v6                       |

健康探测（经 Nginx 统一入口）：

```
GET http://127.0.0.1:2026/health       → 200
GET http://127.0.0.1:2026/health/ready → {"status":"ready","service":"deer-flow-gateway","database":"ok","checkpointer":"ok"}
```

### 3.2 数据目录与持久化状态

| 项                 | 实测值                                                                     |
| ----------------- | ----------------------------------------------------------------------- |
| `DEER_FLOW_HOME`  | `backend/.deer-flow/`（默认值，与 `feature-inventory.md` §8.2 一致）              |
| SQLite 库          | `backend/.deer-flow/data/deerflow.db`（另有 1.3 MB WAL）                     |
| Alembic head      | `0023_user_preferences`                                                 |
| LangGraph store 迁移 | `store_migrations` = 0,1,2,3,4（5 条）                                    |
| 表总数               | 25                                                                      |
| **有数据的表**         | **仅 3 张**：`alembic_version`(1)、`store_migrations`(5)、`users`(1)            |
| 空表                | `threads_meta`、`runs`、`checkpoints`、`writes`、`run_events` 等**全部为空**       |
| 用户                | `demo@example.com`（`3b4ff60a-…`，auth 已启用）                               |

> 表名为 `threads_meta`（复数），不是 `thread_meta`。
>
> 上述为 **L2 执行前（12:51）** 的状态，是"主链路从未跑过"这一审计结论的原始证据。执行后的状态见 §5。

### 3.3 ⚠️ 主链路从未真正跑通过（**L2 执行前的历史状态**）

> 这是 Phase 0 审计阶段（12:51）的取证结论，也是"0.3 未完成"的直接依据。**该状态已于 12:57 由 L2 执行终结**，见 §5。

- `logs/gateway.log`（132 行）中 `POST /api/threads/{id}/runs` 命中数 = **0**。
- 日志里只有前端浏览痕迹：`/api/features`、`/api/skills`、`/api/models`、`/api/channels/providers`、`/api/v1/auth/me`、`POST /api/threads/search`。
- 数据库侧印证：`threads_meta` 与 `runs` / `checkpoints` 表**全空**，无任何 thread 或 run 落库。

→ **本环境此前从未执行过一次完整 run**，因此"thread → run → SSE → checkpoint → resume"这条链路**没有任何历史基线可比**。Phase 0.3 缺的不只是一份清单，而是**一次成功的基线执行**。

### 3.4 工具链版本

| 工具             | 版本 / 路径                                                          |
| -------------- | -------------------------------------------------------------- |
| `uv`           | 0.11.8（Homebrew `/opt/homebrew/bin/uv`）                          |
| Backend Python | 3.12.13（`backend/.venv`）                                        |
| Node           | v22.22.2                                                       |

> **环境坑**：Bash 工具的 PATH **不含 `/opt/homebrew/bin`**，裸写 `uv` 会 `command not found`。跑后端测试必须用绝对路径：
> `cd backend && PATH="/opt/homebrew/bin:$PATH" PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 /opt/homebrew/bin/uv run pytest …`

## 4. 0.2 启用能力清单的落位

`feature-inventory.md` **§5（Feature Inventory，L554–820）** 即 0.2 的产出物：12 个小节，每行带 `Runtime Critical` / `Classification` / `Risk`，且**"默认"列明确指"当前仓库 `config.yaml` 的实际生效状态"**（§5 开头说明 L561）。§15.2 另按"可直接删除 / 暂缓处理 / 需先解耦 / 暂时保留"四组给出了启用态分组。

本次抽查核对结果（文档 vs 实际配置）：**一致**。

- §5.11「仅 `image_search` 启用，其余 13 家搜索 provider 未启用」↔ 实际 `config.yaml:tools[]` 确实只有 `image_search` + 4 个只读文件工具 ✅
- §5.12「`channel_connections.enabled=false`，`config.yaml` 无 `channels` 键」↔ 实际一致 ✅
- §2.3「23 个 skill」↔ 实际 `skills/public/` 23 个目录 ✅

→ **0.2 判定：已完成**。本文档 §2.1 / §2.2 仅作为"运行时可核对的镜像"，补上文档原本缺的 `config_version` 与运行态快照。

## 5. L2 基线执行结果（2026-09-16 12:57 / 13:00，两轮）

执行 `bash docs/architecture/baseline/smoke-main-chain.sh`，**两轮全绿**（第 1 轮 PASS=15 / FAIL=0 / 46s；收紧断言后第 2 轮 PASS=14 / FAIL=0 / 15s）。明细与判据见 `regression-checklist.md` §3。

### 5.1 落库状态变化（交叉核对，只读 SQLite）

| 表              | L2 前 | 第 1 轮后 | 第 2 轮后 | 说明                                                          |
| -------------- | ---- | ------ | ------ | ----------------------------------------------------------- |
| `threads_meta` | 0    | 1      | 2      | 每次 smoke 新建 1 个 thread                                      |
| `runs`         | 0    | 2      | **4**  | 每次 2 个（首轮 + resume），**4 个全部 `status=success`**             |
| `checkpoints`  | 0    | 50     | 100    | checkpoint 持久化                                              |
| `writes`       | 0    | 60     | 120    | LangGraph pending writes                                    |
| `run_events`   | 0    | 0      | **0**  | 表存在但**始终无行**（`backend=memory`，见 §5.3）                        |
| `users`        | 1    | 2      | 2      | smoke-test 用户只建一次                                            |

### 5.2 模型侧证据（证明不是空跑，且确定性可复现）

| run_id     | first_human_message                    | last_ai_message | llm_calls | in/out tokens |
| ---------- | -------------------------------------- | --------------- | --------- | ------------- |
| `c704b925` | `Reply with exactly the word: pong`    | `pong`          | 1         | 9303 / 3      |
| `11a2cdc7` | `Reply with exactly the word: pong2`   | `pong2`         | 1         | 9331 / 4      |
| `d1b2c3c4` | `Reply with exactly the word: pong`    | `pong`          | 1         | **9303 / 3**  |
| `d76af569` | `Reply with exactly the word: pong2`   | `pong2`         | 1         | **9331 / 4**  |

→ **resume 是真跑通的**，不只是"接口受理"：从 checkpoint 续跑后产出了正确结果 `pong2`。
→ 两轮 token 用量**逐位相同**，说明该基线是**确定性的**，可作为后续阶段的严格比对基准。

### 5.3 一个新发现：`run_events` 的 `memory` 后端是"假持久化"

- `run_events` **表存在**（由 `Base.metadata.create_all()` 建出，25 张表之一），但 `run_events.backend=memory` 下**两轮 smoke 后仍是 0 行**。
- 但 `GET /api/threads/{tid}/runs/{rid}/events` 在 smoke 中**返回了 5 条事件**——它们来自**运行进程的内存事件存储**（`get_run_event_store()`，`routers/thread_runs.py` L1683-1710）。
- **含义**：同一进程生命周期内事件可读，**重启即丢**。任何"事件非空"的断言在 `memory` 后端下都是**假阳性**——它掩盖了审计落库失效。
- **对 Phase 1.5 的直接影响**：Rev.3/Q5 把 `run_events.backend=db` 定为**审计硬需求**，此处正好给出反证——`memory` 下事件根本不落库。切换后回归清单必须把断言从"接口返回非空"升级为"`run_events` 表行数 > 0"。

### 5.4 结论

Phase 0 三项产出**全部闭合**：0.1 快照已冻结、0.2 清单已核对、0.3 回归清单已建立**且基线两轮跑绿**。可以进入 Phase 1。

---

## 6. 基线后的配置漂移（2026-09-16 13:13–13:17）

Phase 0 冻结快照（12:51）之后、Phase 1 正式动工之前，`config.yaml` 与扩展配置发生了两处变化。**记录在此以保证可追溯**——这正是 Phase 0.1 冻结快照的用途。

| 项                        | 变化                                                                  | 行为影响                                                       |
| ------------------------ | ------------------------------------------------------------------- | ---------------------------------------------------------- |
| `extensions_config.json` | **从"不存在"变为存在**（91 B）：`{"middlewares": [], "mcpInterceptors": [], "mcpServers": {}, "skills": {}}` | **无**。文件缺失时 `from_file()` 本就返回等价的空配置（`config/extensions_config.py` L511-513），故生效态完全一致 |
| `config.yaml`            | 新增 L262-263：<br>`stream_bridge:`<br>`  type: memory`                  | **无**。`memory` 本就是代码默认值；此改动只是把**隐式默认显式化**，便于 Phase 1.5 改成 `redis` 时直接改值 |

**哈希对照**：

| 文件                        | 12:51（Phase 0 冻结） | 13:17（当前）                            |
| ------------------------- | ------------------ | ------------------------------------ |
| `config.yaml`             | MD5 `23d7ede2f3d773b8bc156d5aeb7bf0fd`（6442 B） | MD5 `bdcef494442c865bb19221fb27548933`（6472 B） |
| `extensions_config.json`  | 不存在                | 存在，91 B                              |

`config.yaml.frozen` 保留的是 **12:51 的 Phase 0 基线**，**未随之上刷**——基线快照的价值在于"冻结那一刻"，后续变化应以"漂移记录"形式追加（即本节），而不是覆盖快照。

### 6.1 生效态复核（13:32 实测）

- `GET /api/skills` → **skill 总数 23，启用 23，禁用 0**。
  → 印证 `runtime-state.md` §2.1 的结论：`"skills": {}` 等价于"全部默认启用"，**Phase 1.3 仍需显式写入 17 个 `enabled: false`**，光建文件不够。
- `GET /health/ready` → `{"status":"ready","database":"ok","checkpointer":"ok"}`。
- L1 回归集（含 `test_configured_extensions.py`）→ **346 passed / 7 skipped / 11.80s**，全绿。

### 6.2 Phase 1 前置条件更新

原 README 第三节列的三个硬前置，其中两个**已由 Will 解决**：

| # | 前置                                          | 状态                                            |
| - | ------------------------------------------- | --------------------------------------------- |
| 1 | 创建 `extensions_config.json`                  | ✅ 已建（最小干净版，未从 example 派生）                     |
| 2 | `stream_bridge` 无显式键 → 1.5 是"新增"            | ✅ 已显式化为 `memory`，1.5 变成**改值**                 |
| 3 | `config.yaml` / `extensions_config.json` 无 git 锚点 | ⚠️ 仍成立（两者都在 `.gitignore`），依赖本目录快照与本节漂移记录 |

## 7. Phase 0 artifact 补充复核（2026-09-16 14:28）

为闭合 `feature-inventory.md` §17.3 V1 的最后一项 `artifact 可读`，临时在当前配置中只增加了 `write_file` 工具，未启用 `bash`。使用 `SMOKE_VERIFY_ARTIFACT=1` 执行 smoke 后立即恢复配置。

| 项目 | 结果 |
| --- | --- |
| Thread | `a71fbd17-a038-4172-9b62-f1c7760563a0` |
| Run | `d50af974-014e-4151-a30f-d7bef58dd7d3`，`status=success` |
| 生成路径 | `/mnt/user-data/outputs/phase0-artifact.txt` |
| 读取结果 | artifact API HTTP 200 |
| 内容校验 | 包含唯一标记 `PHASE0_ARTIFACT_SMOKE_20260916_1428` |
| 交付校验 | `run.delivery` 包含目标 `present_files` |
| Smoke | `PASS=17 FAIL=0` |
| 配置恢复 | 恢复后 `config.yaml` MD5 `13c965ec9b710c6b618ae721d6803c30` |

至此，Phase 0 的基线、配置清单、主链路、artifact 生成/读取/交付校验均有证据闭合。

## 8. Phase 1 收敛执行结果（2026-09-16 15:00–15:02）

### 8.1 配置结果

Phase 1 的 1.1–1.8 已完成。最终运行配置的 MD5 为 5f0b585c2fdb2aa9978692e48c90c11a，扩展配置 MD5 为 520b4eaf1f9595c8272f64183c57bfbb。两个运行时配置文件仍被 .gitignore 忽略，本目录另存最终快照。

| 项目 | 最终值 / 证据 |
| --- | --- |
| 工具组 | file:read、file:write、bash |
| 移除能力 | image_search、web、browser、knowledge |
| 禁用 skill | extensions_config.json 显式禁用 17 个 |
| scheduler | enabled=true |
| agents API | enabled=true |
| channel connections / MCP tasks | 均为 false |
| task continuity / subagent batches | 保留配置，均为 false，标记“待评估启用” |
| database | postgres，URL 使用 DATABASE_URL |
| run events | db |
| agent storage | db |
| stream bridge | redis，URL 使用 DEER_FLOW_STREAM_BRIDGE_REDIS_URL |

### 8.2 真实基础设施与 L1

本机使用已存在的 postgres-dev（Postgres 15，dev_db）与 redis-dev 容器。切换后：

- GET /health/ready 返回 200，database=ok、checkpointer=ok；
- Postgres 公共表数量为 27；
- L1 配置收敛回归：408 passed / 1 warning；
- Postgres 中实测 runs=2、checkpoints=74、run_events=15，证明 run events 已真实落库，而非仅存在于进程内存。

### 8.3 Phase 1 L2 artifact smoke

使用真实 Postgres/Redis 环境执行：

    SMOKE_VERIFY_ARTIFACT=1
    SMOKE_ARTIFACT_MARKER=PHASE1_POSTGRES_REDIS_ARTIFACT_20260916
    结果：PASS=18  FAIL=0
    Thread: 721f4ead-508d-46b2-8706-6e16502e701e
    首轮 Run: 30edd160-6536-4e7c-8bbc-ad29604dd23c
    Resume Run: ed872b7d-3260-404c-8609-9fc7c452697d

本轮验证了 thread → run → SSE → checkpoint → resume，以及 artifact 生成、artifact API 读取、内容标记校验和 run.delivery.present_files 回执。为让 Agent 生成文件而临时加入的 write_file 已在 smoke 后移除；最终配置恢复并通过哈希核对。
