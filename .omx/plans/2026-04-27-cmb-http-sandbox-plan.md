# CMB HTTP 自定义 Sandbox 扩展计划（community）

## Requirements Summary

1. 在 `community` 目录下新增一个可配置的自定义沙箱实现，通过 HTTP API 连接远端沙箱服务，能力需覆盖 DeerFlow `Sandbox` 抽象要求（`execute_command/read_file/list_dir/write_file/glob/grep/update_file`）。
   - 依据接口约束：`backend/packages/harness/deerflow/sandbox/sandbox.py:6-93`
2. 新实现必须通过 DeerFlow 现有 Provider 生命周期接入（`acquire/get/release` + `get_sandbox_provider()` 动态加载）。
   - 依据扩展点：`backend/packages/harness/deerflow/sandbox/sandbox_provider.py:8-58`
3. 工具层无需改调用方式，继续依赖 `ensure_sandbox_initialized()` 懒加载拿到 Provider 实例。
   - 依据运行链路：`backend/packages/harness/deerflow/sandbox/tools.py:817-873`
4. 需参考 `temp/cmb_sandbox` 的既有逻辑：
   - HTTP API client：`temp/cmb_sandbox/sandbox_api.text:12-304`
   - skill 自动同步：`temp/cmb_sandbox/sandbox_backgroud.txt:25-145`
   - skill 元数据结构：`temp/cmb_sandbox/sanbox_skill_meta.txt:19-136`
5. 需要兼容 DeerFlow 现有上传链路（非挂载模式下通过 `sandbox.update_file` 同步上传文件）。
   - 依据上传逻辑：`backend/app/gateway/routers/uploads.py:101-149`

## Scope And Assumptions

1. 本期只做“接入可用 + 核心能力闭环”，不复刻 AioSandbox 的容器暖池/跨进程锁复杂编排。
   - Aio 复杂编排参考：`backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py:419-704`
2. CMB API 保持 `returnCode == "SUC0000"` 语义、并可访问 `/instance`、`/v1/shell/*`、`/v1/file/*`。
   - 来源：`temp/cmb_sandbox/sandbox_api.text:89-304`
3. `sandbox` 配置可直接扩展自定义字段（无需改 Pydantic schema）。
   - 依据：`backend/packages/harness/deerflow/config/sandbox_config.py:83`

## Acceptance Criteria

1. 配置 `sandbox.use: deerflow.community.cmb_sandbox:CmbSandboxProvider` 后，`get_sandbox_provider()` 可成功实例化并为工具层返回可用 sandbox。
2. `bash/ls/read_file/write_file/glob/grep/update_file` 全部可在 CMB 沙箱模式运行（至少具备单测覆盖 + 本地冒烟覆盖）。
3. 在 `uses_thread_data_mounts = False` 模式下，上传接口会调用 `sandbox.update_file()` 将上传文件同步到远端沙箱。
4. 在含 skill 路径命令场景下，可触发自动同步（一次上传，多次复用，避免重复传输）。
5. 配置文档中明确新增 CMB provider 用法、必填字段、鉴权字段来源（环境变量）。

## Implementation Steps

1. 新增 community 模块骨架（`cmb_sandbox`）
   - 新建：
     - `backend/packages/harness/deerflow/community/cmb_sandbox/__init__.py`
     - `backend/packages/harness/deerflow/community/cmb_sandbox/cmb_sandbox.py`
     - `backend/packages/harness/deerflow/community/cmb_sandbox/cmb_sandbox_provider.py`
     - `backend/packages/harness/deerflow/community/cmb_sandbox/client.py`
     - `backend/packages/harness/deerflow/community/cmb_sandbox/skill_sync.py`
   - 在 `__init__.py` 导出 `CmbSandbox` / `CmbSandboxProvider`，保持与 `aio_sandbox` 同风格。
     - 参考导出风格：`backend/packages/harness/deerflow/community/aio_sandbox/__init__.py:1-15`

2. 实现 `CmbSandboxClient`（HTTP API 封装）
   - 以 `temp/cmb_sandbox/sandbox_api.text:63-304` 为蓝本，重构为 DeerFlow 可维护版本（建议统一 `httpx.Client` 或 `httpx.AsyncClient` + 同步包装）。
   - 支持接口：
     - `create_instance`
     - `create_shell_session`
     - `execute_command`
     - `upload_file`
     - `write_file_content`
     - `cleanup`
   - 配置读取从 `get_app_config().sandbox` 获取自定义字段；保留环境变量覆盖优先级。

3. 实现 `CmbSandbox`（对齐 `Sandbox` 抽象）
   - 严格实现接口：`backend/packages/harness/deerflow/sandbox/sandbox.py:18-93`
   - 路径兼容策略：新增 `_map_virtual_path()`，把 DeerFlow 虚拟路径映射到 CMB 根目录（例如 `/mnt/user-data/*` 与 `/mnt/skills/*` 到 `/opt/sandbox/file/*`）。
   - 并发策略：对 shell 执行与写入操作加锁，避免会话并发污染。
     - 参考锁使用：`backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox.py:67-137`
   - `glob/grep` 可先通过远端 shell 命令实现（`find` + `grep`）并解析成 `GrepMatch`。
     - `GrepMatch` 结构依据：`backend/packages/harness/deerflow/sandbox/search.py`

4. 实现 `CmbSandboxProvider`（Provider 生命周期）
   - 对齐 `SandboxProvider` 三方法：`acquire/get/release`。
   - 建议策略：每个 `thread_id` 绑定一个 sandbox 实例 ID（可沿用 deterministic hash 思路），`release` 默认保留实例并更新时间戳，提供可选 idle 回收线程。
     - 参考 deterministic 思路：`backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py:238-245`
   - `uses_thread_data_mounts = False`，确保上传路由走远端同步逻辑。
     - 上传路由判断点：`backend/app/gateway/routers/uploads.py:57-107`

5. 接入 skill 自动同步（来自 temp 逻辑）
   - 复用 `temp/cmb_sandbox/sandbox_backgroud.txt:42-124` 的核心思路：
     - 识别命令中的 skill 路径签名
     - 未同步时打包 zip 上传并在沙箱解压
   - 技术实现改造：
     - 从 DeerFlow `skills.loader.load_skills()` 获取技能目录与元信息
       - 参考：`backend/packages/harness/deerflow/skills/loader.py:25-102`
     - 保证路径映射与 DeerFlow 默认 `skills.container_path` (`/mnt/skills`) 一致
       - 参考：`backend/packages/harness/deerflow/config/skills_config.py:18-21`

6. 配置与文档补充
   - 更新 `config.example.yaml`，新增 Option 4（CMB HTTP Sandbox）示例配置块。
     - 当前 sandbox 示例区：`config.example.yaml:476-567`
   - 在 `backend/README.md` sandbox provider 说明中补充 `CmbSandboxProvider`。
     - 当前 provider 描述：`backend/README.md:72-83`
   - 可选：在 `docs/analysis/sandbox_architecture.md` 的扩展章节补一段 CMB 实现说明。

7. 测试实现
   - 新增测试文件：
     - `backend/tests/test_cmb_sandbox.py`
     - `backend/tests/test_cmb_sandbox_provider.py`
     - `backend/tests/test_cmb_sandbox_client.py`
     - `backend/tests/test_cmb_skill_sync.py`
   - 关键用例：
     - Provider acquire/get/release 与 thread 复用
     - 路径映射正确性（`/mnt/user-data/*` 与 `/mnt/skills/*`）
     - `update_file` 二进制上传正确性（被上传路由调用）
       - 上传依赖点：`backend/app/gateway/routers/uploads.py:126-149`
     - skill 首次同步与去重

8. 联调与回归验证
   - 配置切换到 `CmbSandboxProvider` 后，执行工具级 smoke：`bash/ls/read_file/write_file/glob/grep`。
   - 回归已有关联测试（尤其上传与 sandbox 相关测试）：
     - `backend/tests/test_uploads_router.py`
     - `backend/tests/test_sandbox_search_tools.py`
     - `backend/tests/test_sandbox_tools_security.py`

## Risks And Mitigations

1. 风险：同步/异步模型不一致
   - 原 temp 实现是 async，而 DeerFlow `Sandbox` 抽象是 sync。
   - 缓解：统一封装为同步 API（或单点 event-loop bridge），并在单测中覆盖并发路径。

2. 风险：虚拟路径不一致导致工具不可用
   - DeerFlow 工具层对非 local sandbox 不做路径替换：`backend/packages/harness/deerflow/sandbox/tools.py:889-910`
   - 缓解：`CmbSandbox` 内部强制做虚拟路径映射，保证 `/mnt/...` 输入可执行。

3. 风险：上传链路文件不可见
   - 该 provider 不走挂载模式，必须依赖 `update_file` 同步。
   - 缓解：固定 `uses_thread_data_mounts=False`，并添加上传路由联调用例。

4. 风险：skill 同步重复上传与并发冲突
   - 缓解：引入 `uploaded_skills` 集合 + `RLock`（与 temp 逻辑一致）。
     - 参考：`temp/cmb_sandbox/sandbox_backgroud.txt:39-51`

5. 风险：API 认证配置泄露或硬编码
   - 缓解：配置只支持环境变量注入，文档禁止明文 key；日志中脱敏。

## Verification Steps

1. 代码静态检查
   - `cd /home/fweil/gitprojects/deer-flow/backend && uv run ruff check .`
2. 目标单测
   - `cd /home/fweil/gitprojects/deer-flow/backend && uv run pytest tests/test_cmb_sandbox_client.py tests/test_cmb_sandbox.py tests/test_cmb_sandbox_provider.py tests/test_cmb_skill_sync.py`
3. 关键回归
   - `cd /home/fweil/gitprojects/deer-flow/backend && uv run pytest tests/test_uploads_router.py tests/test_sandbox_search_tools.py tests/test_sandbox_tools_security.py`
4. 运行时冒烟（切换 config 到 CMB provider）
   - 在真实线程中验证：`bash -> write_file -> read_file -> glob -> grep -> 上传文件后读取`

## Out Of Scope（本轮不做）

1. 不引入与 Aio 完全同级的多进程文件锁 + 暖池容器编排。
2. 不改动 Docker 启动脚本模式识别（`scripts/docker.sh` 对未知 provider 默认 local，符合 CMB 远端 HTTP 场景）。
   - 参考：`scripts/docker.sh:50-60`

