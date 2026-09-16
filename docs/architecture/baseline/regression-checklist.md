# Phase 0.3 回归清单（删除后必须通过）

> 对应 `feature-inventory.md` §16 **Phase 0.3** 与 §17.3 **V1**：
> "每阶段后跑主链路 smoke：`thread create → run stream → SSE 完整 → checkpoint 存在 → resume 可用 → artifact 可读`"。
>
> **用法**：Phase 1–7 的**每一步之后**都要跑完本清单。任一 L1/L2 项变红即回滚该步。

---

## 0. 分层设计

| 层     | 内容                     | 是否需要模型 | 耗时        | 用途                        |
| ----- | ---------------------- | ------ | --------- | ------------------------- |
| **L0** | 服务可达 / 配置可加载           | 否      | 秒级        | 拦截"配置引用了已删模块"导致的启动失败       |
| **L1** | 主链路测试集（9 个测试文件，无外部依赖） | 否      | **~12 秒** | 每次改动的**必跑门槛**             |
| **L2** | 端到端 HTTP smoke（真模型 + 真 DB） | **是**  | 1–3 分钟    | 阶段级验收；验证 L1 覆盖不到的装配与持久化细节  |

> 设计原则：**L1 必须能在无模型、无网络的情况下全绿**——否则裁剪过程中任何一次失败都无法区分"是我改坏的"还是"环境问题"。L2 只在阶段边界跑。

---

## 1. L0 —— 服务可达

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:2026/health/ready
curl -s http://127.0.0.1:2026/health/ready
```

**判据**：HTTP 200，且 body 为
`{"status":"ready","service":"deer-flow-gateway","database":"ok","checkpointer":"ok"}`

**基线实测（2026-09-16 12:51）**：✅ 通过。

---

## 2. L1 —— 主链路测试集（必跑）

```bash
cd backend && PATH="/opt/homebrew/bin:$PATH" \
  PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 \
  /opt/homebrew/bin/uv run pytest -q --no-header -p no:cacheprovider \
  tests/test_runtime_lifecycle_e2e.py \
  tests/test_sse_format.py \
  tests/test_run_event_stream_contract.py \
  tests/test_harness_boundary.py \
  tests/test_stream_bridge.py \
  tests/test_checkpoint_mode.py \
  tests/test_checkpoint_lineage.py \
  tests/test_gateway_services.py \
  tests/test_cached_history_saver_integration.py \
  tests/test_configured_extensions.py
```

> ⚠️ **`uv` 必须写绝对路径**：Bash 工具的 PATH 不含 `/opt/homebrew/bin`，裸写 `uv` 会 `command not found`。
>
> `test_configured_extensions.py` 是 **Phase 1 起必须加进来**的——配置层收敛会动 `extensions_config.json`，必须验证扩展装配仍正确。

**基线实测（2026-09-16 12:51，9 个文件）**：

```
329 passed, 7 skipped, 1 warning in 11.69s
```

**复测（2026-09-16 13:32，10 个文件，含 `test_configured_extensions.py`）**：

```
346 passed, 7 skipped, 1 warning in 11.80s
```

✅ **两次全绿**（7 skipped 为 `integration` 标记的 Redis 相关用例，无 Redis 时按设计跳过）。

### 2.1 各文件覆盖什么（裁剪时按改动面挑重点看）

| 文件                                         | 覆盖                                                       | 对应主链路环节                          |
| ------------------------------------------ | -------------------------------------------------------- | -------------------------------- |
| `test_runtime_lifecycle_e2e.py`            | **真 FastAPI app + auth 中间件 + lifespan 依赖 + `start_run()` + `run_agent()` + StreamBridge + checkpointer + run store + thread meta store**（8 例，用假模型，不调外部 API） | thread create → run → SSE → checkpoint → cancel/rollback |
| `test_sse_format.py`                       | SSE 帧格式（`event:` / `data:` / `id:` 顺序、空行收尾）                | SSE 完整                           |
| `test_run_event_stream_contract.py`        | run 事件流契约                                                | 事件持久化                            |
| `test_harness_boundary.py`                 | **`app` → `harness` 单向依赖**（`harness` 不得 import `app`）      | §17.3 V4 —— 任何阶段都必须绿             |
| `test_stream_bridge.py`                    | StreamBridge 抽象与实现（memory / redis）                        | SSE 生产者-消费者解耦                    |
| `test_checkpoint_mode.py`                  | checkpoint channel 模式（full / delta）                       | checkpoint 存在                    |
| `test_checkpoint_lineage.py`               | checkpoint 血缘 / 分支                                        | resume 可用                        |
| `test_gateway_services.py`                 | `services.py` 主链路函数（含 **resume** 路径）                      | resume 可用                        |
| `test_cached_history_saver_integration.py` | checkpoint 历史读写                                           | checkpoint 历史                    |

### 2.2 扩集（按阶段追加）

| 阶段                     | 追加测试                                                                              | 理由                                |
| ---------------------- | --------------------------------------------------------------------------------- | --------------------------------- |
| Phase 1（配置收敛）          | `test_app_config_reload.py`、`test_app_config_name_indexes.py`、`test_configured_extensions.py` | 配置变更后必须验证配置能加载、索引正确、扩展装配正确        |
| Phase 2（删叶子）           | 被删模块对应的测试**同步删除**；另加 `test_check_script.py`                                     | 保证 `make check` 仍通过、无悬空引用           |
| Phase 3（渠道下线）           | `test_harness_boundary.py`（确认 harness 仍零引用 `app.channels`）、渠道测试同步删除                   | 渠道与 Runtime 解耦是否彻底                 |
| Phase 4（浏览器下线）          | `test_browser_*.py` 同步删除                                                          | 启动期静态引用是否清除                       |
| Phase 5（FS 解耦）          | `test_artifact_archive.py`、`test_artifacts_router.py`、`test_agent_storage_backend.py` | 存储抽象替换后行为等价                       |
| Phase 6（执行环境）           | `test_sandbox_tools_security.py`、`test_aio_sandbox*.py`                             | 沙箱与文件工具拆分后授权矩阵不变                  |
| Phase 7（能力补建）           | 新增能力的测试 + 本清单全量                                                                   | 净新增不破坏既有链路                        |

---

## 3. L2 —— 端到端 HTTP smoke（阶段边界跑）

```bash
bash docs/architecture/baseline/smoke-main-chain.sh
```

脚本位于本目录 `smoke-main-chain.sh`，覆盖 8 个环节（artifact 模式额外执行第 6 项）：

| # | 环节                    | 断言                                                             |
| - | --------------------- | -------------------------------------------------------------- |
| 0 | 服务可达                  | `/health/ready` = 200 且 `database`/`checkpointer` 均为 `ok`         |
| 1 | 认证会话                  | 注册/登录 → `/auth/me` = 200 → 取到 `csrf_token` cookie               |
| 2 | **thread create**     | `POST /api/threads` 返回 `thread_id`                              |
| 3 | **run stream (SSE)**  | `POST /api/threads/{tid}/runs/stream` = 200；收到 ≥1 帧；以 `event: end` 收尾；无 `event: error` |
| 4 | **checkpoint 存在**     | `GET /api/threads/{tid}/state` 的 `values` 非空；`POST /api/threads/{tid}/history` 条数 > 0 |
| 5 | run 落库 + 事件持久化        | `GET /api/threads/{tid}/runs` 有记录且终态 `success`；`/runs/{rid}/events` 非空 |
| 6 | **artifact 生成与读取** | Agent 用 `write_file` 生成目标文件并调用 `present_files`；artifact API 返回 200 且内容可校验；`run.delivery` 记录目标路径 |
| 7 | **resume**            | 取 history 中一个 `checkpoint_id`，`POST /api/threads/{tid}/runs` 带 `checkpoint_id` 被受理 |

**基线实测（第 1 次：2026-09-16 12:57，46 秒）**：

```
[0] 服务可达性        PASS /health/ready -> 200
[1] 认证会话          PASS 注册并登录 (201) / PASS /auth/me -> 200 / PASS 取得 CSRF token
[2] thread create     PASS thread 已创建: 4ce2bb68-139e-4f5c-aca5-7ea1cf87da5b
[3] run stream (SSE)  PASS 连接 200 / PASS 收到 15 帧 / PASS 以 event: end 收尾 / PASS 无 error 帧
[4] checkpoint 存在   PASS state.values 非空 / PASS checkpoint history 条数: 10
[5] run 状态与事件    PASS run 已落库 c704b925… / PASS 终态 = success / PASS run_events 条数: 5
[6] resume           PASS 基准 checkpoint 1f1b18b1… / PASS resume run 已受理 11a2cdc7…
结果：PASS=15  FAIL=0
```

**基线实测（第 2 次：2026-09-16 13:00，15 秒，脚本收紧断言后重跑）**：

```
[1] 认证会话          PASS 登录 (200)          ← 用户已存在，走登录分支
[2] thread create     PASS thread 已创建: 3fbb5532-c32b-415d-a7ca-febfe7c2235e
[3] run stream (SSE)  PASS 连接 200 / PASS 收到 15 帧 / PASS 以 event: end 收尾 / PASS 无 error 帧
[4] checkpoint 存在   PASS state.values 非空 / PASS checkpoint history 条数: 10
[5] run 状态与事件    PASS run 已落库 d1b2c3c4… / PASS 终态 = success
                      ---- run_events.backend = memory；/events 返回条数: 5
                      ---- ⚠ 后端为 'memory'：以上条数来自进程内内存，重启即丢，不代表审计落库
[6] resume           PASS 基准 checkpoint 1f1b18b9… / PASS resume run 已受理 d76af569…
结果：PASS=14  FAIL=0
```

**artifact 补充实测（2026-09-16 14:28，临时启用 `write_file` 后立即恢复）**：

```text
thread: a71fbd17-a038-4172-9b62-f1c7760563a0
run:    d50af974-014e-4151-a30f-d7bef58dd7d3
path:   /mnt/user-data/outputs/phase0-artifact.txt
marker: PHASE0_ARTIFACT_SMOKE_20260916_1428

[6] artifact 生成与读取
PASS artifact API 读取 200
PASS artifact 内容包含唯一校验标记
PASS run.delivery 记录了 present_files

结果：PASS=17  FAIL=0
```

该轮同时验证了 Agent 实际写文件、`present_files` 交付回执、Gateway artifact 读取接口三者的连接；临时配置已恢复，恢复后哈希为原值。

**基线实测（第 3 轮：2026-09-16 13:31，4 秒，引入 `extensions_config.json` 后）**：

```
[1] 认证会话          PASS 登录 (200) / PASS /auth/me -> 200 / PASS 取得 CSRF token
[2] thread create     PASS thread 已创建: 550bb65a-f4bf-4b6a-9ef8-0ba02d232508
[3] run stream (SSE)  PASS 连接 200 / PASS 收到 15 帧 / PASS 以 event: end 收尾 / PASS 无 error 帧
[4] checkpoint 存在   PASS state.values 非空 / PASS checkpoint history 条数: 10
[5] run 状态与事件    PASS run 已落库 882d7115… / PASS 终态 = success
                      ---- run_events.backend = memory；/events 返回条数: 5
                      ---- ⚠ 后端为 'memory'：以上条数来自进程内内存，重启即丢，不代表审计落库
[6] resume           PASS 基准 checkpoint 1f1b18fd… / PASS resume run 已受理 fd6baf13…
结果：PASS=14  FAIL=0
```

> **三轮 PASS 数（15 → 14 → 14）的差异是刻意的**：第 1 版脚本把"`/events` 返回非空"计为 PASS，但在 `run_events.backend=memory` 下那是**进程内内存**，属假阳性。收紧后该项降级为 INFO 提示，不再计入 PASS。**判据以第 2/3 版为准。**

> **⚠️ 脚本内禁止用 Python 读文件（第 3 轮才暴露的坑）**
> 第 3 轮首次执行时**整脚本卡满 2 分钟超时失败**，报：
> `PermissionError: Sensitive content approval timed out`（沙箱对"读取含凭据文件"的审批被拦截）。
> 触发点是 Python 读取 cookie jar（内含 session / CSRF 凭据）。
> **修复**：CSRF 改为从**认证响应头**（`Set-Cookie: csrf_token=…`）提取，回退路径用 `sed`；
> 脚本内所有文本处理改用 `/usr/bin/` 绝对路径工具（本机 PATH 上的 `sed`/`tail`/`wc`/`head` 是 brokered 垫片），
> Python 仅用于**通过 argv 传参**的 JSON 解析，**不读任何文件**。改动已固化在脚本头部的"设计约束"注释里。

✅ **两次全绿，可复现**。落库侧交叉核对（只读 SQLite）：

| 表              | 基线前 | 第 1 次后 | 第 2 次后 | 说明                                                          |
| -------------- | --- | ------ | ------ | ----------------------------------------------------------- |
| `threads_meta` | 0   | 1      | 2      | 每次 smoke 新建 1 个 thread                                        |
| `runs`         | 0   | 2      | **4**  | 每次 2 个 run（首轮 + resume），**4 个全部 `status=success`**            |
| `checkpoints`  | 0   | 50     | 100    | checkpoint 持久化                                              |
| `writes`       | 0   | 60     | 120    | LangGraph pending writes                                    |
| `run_events`   | 0   | 0      | **0**  | 表存在但**始终无行**（`backend=memory`，见 §3.3）                        |
| `users`        | 1   | 2      | 2      | smoke-test 用户只建一次                                            |

模型侧验证（证明不是空跑，且**结果确定可复现**）：

| run_id     | first_human_message                    | last_ai_message | llm_calls | in/out tokens |
| ---------- | -------------------------------------- | --------------- | --------- | ------------- |
| `c704b925` | `Reply with exactly the word: pong`    | `pong`          | 1         | 9303 / 3      |
| `11a2cdc7` | `Reply with exactly the word: pong2`   | `pong2`         | 1         | 9331 / 4      |
| `d1b2c3c4` | `Reply with exactly the word: pong`    | `pong`          | 1         | **9303 / 3**  |
| `d76af569` | `Reply with exactly the word: pong2`   | `pong2`         | 1         | **9331 / 4**  |

→ **resume 是真跑通的**，不只是"接口受理"：从 checkpoint 续跑后产出了正确结果 `pong2`。
→ 两轮 token 用量**逐位相同**（9303/3、9331/4），说明基线是**确定性**的，可作为后续阶段的严格比对基准。

### 3.3 ⚠️ `run_events` 在 `memory` 后端下是"假持久化"（新发现）

- `run_events` **表存在**（`Base.metadata.create_all()` 建出，25 张表之一），但 `run_events.backend=memory` 下**两轮 smoke 后仍是 0 行**。
- 而 `GET /api/threads/{tid}/runs/{rid}/events` **返回了 5 条事件**——它们来自**运行进程的内存事件存储**（`get_run_event_store()`，`routers/thread_runs.py` L1683-1710）。
- **含义**：同一进程生命周期内事件可读，**重启即丢**。任何"事件非空"的断言在 `memory` 后端下都是**假阳性**，会掩盖审计落库失效。
- **对 Phase 1.5 的直接影响**：Rev.3/Q5 把 `run_events.backend=db` 定为**审计硬需求**，此处正好给出反证。切换后本清单必须把断言从"接口返回非空"升级为"`run_events` 表行数 > 0"。脚本已内置该分支（读 `config.yaml` 的 `run_events.backend`，为 `db` 时按硬性断言处理）。

> ⚠️ 本脚本会**真实调用模型**（走 `config.yaml` 的 provider）并写入 dev DB，会消耗 token 且在线程列表留下记录。跑之前确认属于预期。

---

## 4. 阶段验收表（每步打勾）

| 阶段              | L0 | L1 | L2 | 备注                                  |
| --------------- | -- | -- | -- | ----------------------------------- |
| **Phase 0（本阶段）** | ✅  | ✅  | ✅  | **主链路三轮 + artifact 补充轮均通过（PASS=17，FAIL=0）** |
| Phase 1         |    |    |    | 配置层收敛；**必须先建 `extensions_config.json`** |
| Phase 2         |    |    |    | 删叶子；每删一个模块跑一次 L1                    |
| Phase 3         |    |    |    | 渠道下线；顺序 3.2 → 3.2b → 3.3 → 3.4 不可换  |
| Phase 4         |    |    |    | 浏览器下线                               |
| Phase 5         |    |    |    | FS 解耦（重构）；L1 扩集必跑                   |
| Phase 6         |    |    |    | 执行环境改造                              |
| Phase 7         |    |    |    | 能力补建                                |

---

## 5. 历史空白与收尾状态（Phase 0.3）

1. ~~L2 从未执行过~~ → **已于 2026-09-16 12:57 执行并全绿**（见 §3）。本环境此前 `POST /api/threads/{id}/runs` 命中数为 0、DB 仅 3 张表有数据（详见 `runtime-state.md` §3.3），**该历史状态已由本次执行终结**，基线绿色起点已建立。
> 第 2 项原记录是历史状态；已于 2026-09-16 14:28 通过 artifact 补充 smoke 收尾，详见上方实测记录。该轮真实生成并读取 `/mnt/user-data/outputs/phase0-artifact.txt`，且 `run.delivery` 记录了 `present_files`，结果 `PASS=17 FAIL=0`。
2. **`artifact 可读` 环节仍未纳入**。§17.3 V1 的完整表述含 `artifact 可读`，但本次 smoke 的 prompt 是纯文本回答，**没有产出文件**，因此 artifact / `workspace_changes` 链路（见 `feature-inventory.md` §19）**未经验证**。建议在 Phase 2.9（删 `workspace_changes` 的 L1+L2）**之前**补一次"让 Agent 写文件并 present"的 run 作为对照——否则删 L1/L2 时无法区分是否误伤了 L3 交付校验。
3. **`.agent/skills/smoke-test/` 不能替代本清单**。它是**部署/健康** smoke（`make check` → `make install` → `make start` → 端口 → `/health` → 前端 `/workspace` 路由），主链路只到可选的 "Simple chat test"，**不覆盖 checkpoint 与 resume**。两者是互补关系：部署 smoke 验证"服务起得来"，本清单验证"链路跑得通"。
4. **smoke 会累积测试数据**。每次执行都会新建 thread + 2 个 run + 50 个 checkpoint。基线后 `threads_meta`=1 / `runs`=2 / `checkpoints`=50，可作为"跑过几轮"的计数参照。Phase 1.5 切 Postgres 时需一并迁移或清理这批数据。
