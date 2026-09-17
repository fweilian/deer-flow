# Goal 1 — Object Storage Foundation

基于最新 `phase5-filesystem-rescan.md`，实现 Phase 5 的 Object Storage Foundation。

这是 Phase 5 的基础设施任务。只实现共享 Object Storage 基础能力，不提前迁移 Artifact、Uploads、`.tool-results`、Custom Skills，也不要扩大到 Workspace、Memory、Checkpoint、Channel Attachment 或整个 `config/paths.py`。

## Scope

实现：

1. S3-compatible Object Storage 配置模型。
2. 使用 `aiobotocore` 实现 async S3 client。
3. 建立最小、明确的 shared storage port / abstraction。
4. 支持后续 Phase 5 所需的基础操作，包括：

   * streaming read
   * streaming write
   * stat / metadata
   * range read
   * list by prefix
   * delete
   * copy，如生命周期实现确实需要
   * checksum / etag 相关语义，保持后续 Artifact 验证需要
5. 定义稳定的 object key namespace，至少能够承载：

   * outputs / artifacts
   * uploads
   * `.tool-results`
   * custom skills / skill state
6. Object Storage 不可用时必须失败，不允许静默回退本地磁盘。
7. FastAPI / asyncio 正常路径不得直接调用阻塞式 S3 SDK。
8. Docker/dev compose 增加 MinIO 测试环境：

   * endpoint
   * bucket
   * access key / secret key
   * region
   * path-style
   * healthcheck
   * bucket initialization
   * named volume
9. TLS 本阶段不要求。
10. 不做 backfill、dual-read、dual-write。

## Architecture constraints

* 不要为了 Phase 5 抽象整个 filesystem。
* 不要重构与本 Goal 无关的 `config/paths.py` 调用。
* Object Storage 是 Phase 5 持久文件的 Source of Truth。
* 本地磁盘只能用于明确允许的临时 / derived cache，不得成为持久回退。
* API 必须优先支持 streaming，避免把完整文件加载进内存。
* abstraction 应保持窄，不要设计通用云存储框架或 speculative extension points。
* 遵循 KISS / DRY，不为未来可能需求增加接口。

## Verification strategy — IMPORTANT

严格控制测试数量和测试执行次数。

开始实现前：

* 先阅读现有相关测试和调用链。
* 不要先跑整个 backend test suite。
* 不要为了“建立信心”执行所有 Phase 5 无关测试。

开发过程中：

* 只运行与你刚修改的 Object Storage config / client / port 直接相关的最小测试。
* 尽量一次完成一组相关修改后再运行测试，不要每修改一个文件就重新执行。
* 如果已有测试已经覆盖某个公共契约，只调整现有测试，不重复新增同义测试。
* 不测试 aiobotocore / MinIO / FastAPI / Python 标准库本身的行为。
* 不为静态类型已经保证的情况增加运行时测试。
* 不做穷举 error matrix。
* 只覆盖：

  1. 我们自己的 storage contract；
  2. streaming / range 等容易回归的公共语义；
  3. Object Storage 不可用时禁止 local fallback；
  4. 必要的 MinIO integration smoke。

Goal 收尾时：

* 集中执行一次 Goal 1 的相关测试。
* 执行必要的 Ruff / mypy，仅针对实际受影响范围或项目现有门禁要求。
* 不执行 Phase 5 全量双实例验收；该验收留给 Goal 5。
* 不因为本 Goal 修改 storage foundation 就运行所有 Artifact / Upload / Skill 测试。

## Completion criteria

完成后应具备稳定、可复用的 async S3-compatible Object Storage 基础能力，使后续 Goal 可以直接迁移各文件域，而无需再次设计 storage abstraction。

最后输出：

1. 实现摘要
2. 新增/修改的 storage contract
3. object key namespace
4. 配置项
5. 实际运行的测试/检查及结果
6. 明确列出没有运行的全量测试，说明因为它们不属于本 Goal
7. 留给 Goal 2–5 的接口与注意事项

# Goal 2 — Outputs / Artifacts / Tool Results Migration

基于最新 `phase5-filesystem-rescan.md` 和 Goal 1 已完成的 Object Storage Foundation，将 Outputs / Artifacts 和 `.tool-results` 迁移到共享 Object Storage。

这是 Phase 5 风险最高的迁移之一。必须保持现有对外语义和 Run 交付判定，不要借机重写整个 Artifact 系统。

## Scope

迁移以下能力：

### Outputs / Artifacts

覆盖：

* Artifact GET
* Artifact PUT / write path
* download
* range request
* stat / metadata
* checksum / etag 相关语义
* archive / zip，如当前 API 支持
* `present_files`
* outputs listing
* outputs confined-path 对应的安全语义
* run worker 中依赖 outputs 的最终交付确认 / verification
* branch / delete 暂不在本 Goal 完整实现，由 Goal 5 统一做生命周期收口

### `.tool-results`

迁移：

* `ToolOutputBudgetMiddleware`
* 超长 Tool Output externalization
* 后续读取 externalized output 的路径
* `.tool-results` 继续作为共享 outputs namespace 内部区域，不单独设计新的 Artifact 类型

## Required behavior

* Object Storage 是 Outputs / Artifact / `.tool-results` 的唯一持久 Source of Truth。
* 不允许正常请求路径继续依赖 thread 本地 `outputs/` 或 `.tool-results/`。
* 不允许 Object Storage 失败后回退本地文件。
* 下载和 range response 必须 streaming。
* 不要整文件读入内存后再返回。
* 保持现有 API contract，除非最新 rescan 明确要求改变。
* 保持 path confinement / key validation，不能因为迁移到 object key 就降低安全边界。
* `present_files` 必须继续只允许声明合法 outputs。
* Run 终态判定如果依赖 output existence / changed outputs，必须改为新的 shared outputs abstraction，不能简单删除该检查。
* 不要扩大到 Uploads、Custom Skills、Workspace、Memory。

## Implementation guidance

优先让 Artifact API、worker verification、`present_files`、tool-output middleware 共享同一 outputs storage port。

避免：

* Artifact API 自己直接调用 S3 client；
* worker 再实现一套 S3 访问；
* `.tool-results` 再实现第三套 storage helper；
* 为兼容旧本地文件做 dual-read；
* 在 request handler 中创建 tempfile 再上传。

如果某些 ZIP/archive 操作确实需要临时文件，必须：

* 明确它只是 request-scoped temporary data；
* 不是 Source of Truth；
* 生命周期结束后清理；
* 不因此重新引入持久本地目录依赖。

## Verification strategy — IMPORTANT

测试数量和执行次数必须严格控制。

开始前：

* 先定位现有 Artifact、present_files、run output verification、ToolOutputBudget 的测试。
* 不运行 backend 全量测试。

开发中：

* 按一个逻辑批次完成修改后再跑定向测试。
* 优先修改现有测试，而不是重新建立第二套 Object Storage 测试矩阵。
* 不要分别为 GET、PUT、range、archive 在多个层重复测试同一个 storage contract。
* Storage 基础行为如果 Goal 1 已覆盖，本 Goal 只测试 Artifact 层自己的语义。
* 不测试 MinIO / aiobotocore 框架行为。
* 不测试理论上不会通过 public API 形成的非法状态。
* 不为简单 delegation 写单测。

最小需要覆盖：

1. Artifact 写入后可读取。
2. range 语义保持。
3. `present_files` 与 outputs namespace 正常工作。
4. run output verification 不再依赖持久本地 outputs。
5. `.tool-results` externalization 和读取可跨实例工作所需的 storage 语义。
6. Object Storage failure 不回退 local disk。

Goal 收尾：

* 一次性跑 Goal 2 相关 Artifact / worker / tool-output tests。
* 必要时做一个真实 MinIO integration smoke。
* 不运行 Upload / Skill 全套测试。
* 不执行最终双 Agent Server acceptance；留给 Goal 5。
* Ruff / mypy 只运行受影响门禁，避免重复执行无关检查。

## Completion criteria

Goal 2 完成后：

* outputs/artifacts 的持久 Source of Truth 已完全切到 Object Storage；
* `.tool-results` 已切到 Object Storage；
* Artifact API 与 Run delivery verification 行为保持；
* 不存在正常运行依赖持久本地 outputs 的路径；
* 不引入 backfill / dual-read / dual-write。

最后输出：

1. 实现摘要
2. 迁移掉的所有 local outputs / `.tool-results` 依赖
3. 保留的临时本地文件用途（如存在）
4. 兼容性说明
5. 实际执行的测试及结果
6. 明确说明为了减少重复验证，没有运行哪些全量测试
7. Goal 5 生命周期仍需处理的事项

# Goal 3 — Uploads Migration

基于最新 `phase5-filesystem-rescan.md` 和 Goal 1 的 Object Storage Foundation，将 Uploads 持久存储迁移到共享 Object Storage。

只处理 Uploads 域，不扩展到 Artifact、Skills、Workspace、Memory 或新的 RAG 架构。

## Scope

迁移：

* upload API
* streaming request body → Object Storage
* list uploaded files
* read uploaded files
* delete uploaded files
* filename validation
* duplicate filename / collision handling
* `claim_unique_filename` 等现有语义
* UploadsMiddleware
* `list_uploaded_files`
* 文档转换流程读取上传文件时的适配
* Sandbox/consumer 如需要文件内容时，从共享 Upload storage 获取或按明确的临时 materialization 规则使用

## Required behavior

上传正常路径必须尽量是：

HTTP request stream
→ Object Storage

不要变成：

HTTP request
→ 完整读取到内存
→ Object Storage

也不要变成：

HTTP request
→ persistent local upload directory
→ background copy to Object Storage

约束：

* Object Storage 是 Upload 的 Source of Truth。
* 不做 dual-write。
* 不做旧本地 upload backfill。
* Object Storage failure 不允许 fallback 到 local persistent upload。
* 保持 upload size limit。
* 保持 filename sanitization / collision 语义。
* 文件转换允许使用 request/job scoped temporary file，如果第三方库明确要求真实 local path。
* temporary file 必须是可清理、非持久、非共享状态。
* 不要求 Sandbox 直接支持 S3 URI；只在现有消费者真正需要 local file 时 materialize。
* 不顺带设计完整 Workspace abstraction。

## Verification strategy — IMPORTANT

减少测试执行次数是本 Goal 的硬要求。

开始前：

* 阅读现有 uploads manager、router、middleware、conversion 和相关测试。
* 不先跑全量测试。

开发过程中：

* 每完成一组相关修改后再执行一次最小定向测试。
* 不要在 router、manager、middleware 三层重复测试同一个 filename/collision 规则。
* 基础 S3 streaming 已由 Goal 1 验证的，不在本 Goal 重复做大量底层测试。
* 优先保留现有 Upload contract tests，并把 backend 从 local 改为 shared storage fixture。
* 不测试 FastAPI multipart parser、aiobotocore、MinIO 自身行为。
* 不做大规模 filename edge-case matrix；只保留真实风险案例。
* 不增加“理论上更完整”但没有历史缺陷或公共契约依据的边界测试。

最小覆盖：

1. 上传成功并能重新读取/list。
2. size limit。
3. duplicate filename / claim unique semantics。
4. delete。
5. UploadsMiddleware / `list_uploaded_files` 能从 shared storage 工作。
6. 必须 local path 的转换流程只使用临时 materialization。
7. Object Storage unavailable 时 fail closed，不回退持久本地 upload。

Goal 收尾时：

* 一次性跑 uploads 相关测试。
* 做最多一个必要的真实 MinIO upload smoke。
* 不跑 Artifact/Skill 完整测试。
* 不跑 Phase 5 最终双实例验收，该工作属于 Goal 5。
* Ruff / mypy 仅运行必要范围。

## Completion criteria

完成后，Upload 的持久状态不再依赖 Agent Server 本地磁盘，并能被多个 Agent Server 实例共享。

最后输出：

1. 实现摘要
2. Upload Source of Truth
3. 临时 materialization 的位置和生命周期
4. 保持的 filename / collision / size-limit 语义
5. 实际执行的测试及结果
6. 为减少重复测试而明确没有运行的检查

# Goal 4 — Custom Skills Shared Storage

基于最新 `phase5-filesystem-rescan.md`，迁移 Custom Skills 及其 state 到共享存储。

必须复用现有 `SkillStorage` abstraction / reflection factory。不要重新建立另一套 Skill repository abstraction。

## Scope

处理：

* Custom Skills 的持久存储
* Custom `SKILL.md`
* Custom Skill package files
* `_skill_states.json` 或其等价持久状态
* Skill create / install / update / delete / enable / disable
* Skill discovery 对 shared backend 的适配
* reflection factory / config wiring
* multi-instance 下的 Custom Skill 可见性

明确不迁移：

* `skills/public/` 代码仓库内静态 skills
* Workspace
* Memory
* Artifact / Upload
* `skills_view` 作为持久 Source of Truth

## Architecture constraints

`skills_view` 继续是：

derived
+
rebuildable
+
local cache/projection

它不是 Source of Truth。

因此：

* 新实例必须能仅依靠 shared SkillStorage 重建所需投影。
* 不允许依赖另一实例已经生成的 `skills_view`。
* 不要把整个 Skill Runtime 改成 S3-aware。
* 尽量让 storage backend 隐藏 Object Storage 细节。
* Skill parser / activation / tool policy 不应因为 storage migration 被大规模重构。
* 不要为未来未知 Skill 类型增加 speculative abstractions。
* 保持现有 SkillStorage contract，只有确有必要才做最小扩展。

## Verification strategy — IMPORTANT

本 Goal 应尤其避免测试膨胀。

开始前：

* 先查看 SkillStorage 已有 contract tests。
* 优先复用它们验证新 backend。
* 不运行所有 public skills tests。

开发中：

* 对 shared SkillStorage backend 做少量 contract-level 测试。
* parser、frontmatter、tool policy 等没有修改的能力不要重新测试。
* 不测试 Object Storage 底层行为；Goal 1 已经覆盖。
* 不为每一种 Skill 生命周期操作重复测试 storage primitives。
* 优先测试业务上真正重要的组合路径，而不是 create/read/update/delete 每一层都复制一套 case。
* 不为 reflection framework 本身写测试。

最小覆盖：

1. Custom Skill create 后另一 storage instance 可读取。
2. update / delete 基本生命周期。
3. enable / disable state 可共享。
4. 新实例可从 shared storage 重建 / 使用 skill，而不依赖旧实例的 `skills_view`。
5. local projection 不成为 Source of Truth。
6. Object Storage failure 不回退到 persistent local Custom Skill directory。

Goal 收尾：

* 一次运行 SkillStorage / custom-skill 相关定向测试。
* 如有必要，做一个双 storage instance 的 lightweight integration test。
* 不运行全部 17/23 个 public skills 测试。
* 不执行整个 backend suite。
* 不执行 Phase 5 最终双 Agent Server MinIO acceptance；交给 Goal 5。
* Ruff / mypy 只跑受影响范围。

## Completion criteria

完成后：

* Custom Skills 和其 enable/state 信息可在多个 Agent Server 间共享；
* `skills_view` 仅为可重建缓存；
* static public skills 不受影响；
* Skill runtime 不需要直接了解 S3。

最后输出：

1. 实现摘要
2. SkillStorage backend 设计
3. Source of Truth 与 derived cache 边界
4. multi-instance 行为
5. 实际执行的测试
6. 明确列出没有执行的无关 Skill 测试

# Goal 5 — Lifecycle, Production Gates and Final Phase 5 Acceptance

基于最新 `phase5-filesystem-rescan.md` 和已经完成的 Goal 1–4，完成 Phase 5 的生命周期、生产配置门禁和最终跨实例验收。

这是 Phase 5 的最终收口 Goal。

不要重新设计 Goal 1–4 已完成的 storage abstraction。先审计已有实现，只补剩余生命周期、配置和验收缺口。

## Scope

### 1. Shared-storage lifecycle

完成所有已经迁移到 Object Storage 的持久文件域的生命周期：

* thread branch / copy
* thread delete
* Artifact / outputs cleanup
* Upload cleanup
* `.tool-results` cleanup
* Custom Skill lifecycle 中仍未覆盖的 cleanup / update
* 必要的 prefix copy / prefix delete

确认不会：

* branch 后仍引用源 thread 的 mutable object；
* delete thread 后留下本应删除的持久 objects；
* 删除 local derived cache 时误删 shared Source of Truth。

### 2. Storage production gate

增加明确的配置门禁：

在要求 multi-instance / shared storage 的生产模式下：

* Object Storage 配置不完整 → startup/readiness 明确失败。
* 不允许静默切回 local persistent filesystem。
* dev/local single-instance 可以按最新文档允许的方式运行，但必须与 production 行为边界清晰。

不要构造复杂 deployment framework，只实现最小可靠 gate。

### 3. USER.md / user-profile cleanup

按照最新 rescan 的范围：

* 删除 Phase 5 已确认不应继续持久化的 `USER.md` / user-profile filesystem path。
* 删除对应 API / config / dead code。
* 不顺带删除 Memory 或其它 user context 能力。

### 4. JWT deployment requirement

生产部署要求：

`AUTH_JWT_SECRET`

必须有明确配置和 fail-fast / readiness 约束。

不要重新设计完整 Auth/AuthZ。

### 5. Final multi-instance acceptance

使用真实：

* Agent Server A
* Agent Server B
* 同一个 MinIO
* 空 bucket 开始

完成 Phase 5 最终验收。

至少验证：

1. A 写 Artifact → B 能读。
2. range/download 正常。
3. A 上传文件 → B 能发现并读取。
4. `.tool-results` 在一个实例 externalize 后，另一实例能够继续消费需要的结果。
5. A 创建/修改 Custom Skill → B 能发现并使用。
6. branch 能正确复制需要独立生命周期的数据。
7. delete 能删除正确范围的 shared objects。
8. 重启任一 Agent Server 后，持久数据仍存在。
9. 删除/重建本地 derived cache 后，不影响持久 Source of Truth。
10. Agent Server 本地持久目录为空或不可依赖时，Phase 5 已迁移能力仍正常工作。
11. Object Storage 不可用时，不发生 local persistent fallback。

## Important scope exclusions

Final acceptance 不代表扩大 Phase 5。

仍然不要迁移：

* Workspace
* Memory
* Checkpoint
* Channel Attachment
* Sandbox runtime filesystem
* 整个 `config/paths.py`

也不要在这个 Goal 开始 MySQL / database migration。

## Verification strategy — CRITICAL

本 Goal 要做最终验证，但仍必须控制重复测试。

开始前：

* 不要重新跑 Goal 1–4 已经通过的所有定向测试。
* 先审计 Goal 1–4 的测试结果和当前 diff。
* 只对本 Goal 新改的 lifecycle / gate / cleanup 做最小定向测试。

开发过程中：

* branch/delete 生命周期应尽量用少量 table-driven / parameterized case 覆盖多个 storage domains，而不是每个 domain 建一套高度重复测试。
* production gate 只验证我们的配置判定，不测试环境变量解析框架本身。
* USER.md/JWT 的机械性删除和配置修改不要大量新增测试。
* 同一个测试集合不要因为连续小修改重复运行；先完成相关修改，再一次执行。
* 不做 exhaustive S3 failure matrix。
* 不测试 MinIO 本身的数据一致性。
* 不重新验证 aiobotocore primitives。

### Final verification order

只在代码基本完成后统一执行：

第一层：

* 本 Goal lifecycle/gate 定向测试。

第二层：

* Phase 5 关键回归：

  * Artifact
  * Upload
  * Tool Output
  * Custom Skill
  * thread lifecycle
  * startup gate

第三层：

* **只执行一次**真实双 Agent Server + MinIO acceptance。

第四层：

* 最后再运行项目要求的静态门禁 / 必要 backend gate。

不要出现：

修改一个文件
→ 全量测试
→ 再修改
→ 再全量测试
→ 再启动双实例
→ 再重复全部测试

目标是：

完成一个逻辑批次
→ 最小定向测试
→ 修正
→ Goal 收尾一次相关回归
→ Phase 5 最终一次跨实例 acceptance

## Test policy

遵循 Risk-Adjusted Verification：

* 使用能提供足够信心的最低测试层。
* 只为 changed behavior、public contract、已复现缺陷或重大边界增加最少的 focused cases。
* 不测试 framework behavior。
* 不测试 static type guarantees。
* 不测试 trivial delegation。
* 不测试理论上不可达状态。
* 避免 duplicate tests。
* 避免 exhaustive test matrices。
* documentation/config cleanup 若无行为变化，不额外增加 application tests。
* 如果现有测试已经足够证明行为，不因为“这是 Phase 5”而增加新的重复测试。

## Final completion criteria

只有同时满足以下条件才认为 Phase 5 完成：

1. Artifact / outputs 持久状态来自 Object Storage。
2. Uploads 持久状态来自 Object Storage。
3. `.tool-results` 持久状态来自 Object Storage。
4. Custom Skills / state 来自 shared storage。
5. `skills_view` 等本地目录仅为 derived/rebuildable cache。
6. branch/delete 生命周期正确。
7. production shared-storage gate 生效。
8. `USER.md` 相关旧 filesystem dependency 已清理。
9. production JWT secret requirement 生效。
10. 双 Agent Server + 空 MinIO bucket 的真实验收通过。
11. 没有依赖旧本地数据、backfill、dual-read 或 dual-write。
12. 没有扩大到 Phase 5 明确排除的文件域。

最后输出：

1. Phase 5 最终实现摘要
2. Source of Truth 清单
3. remaining local filesystem 清单及为什么允许保留
4. branch/delete lifecycle 结果
5. production gate 结果
6. 双实例 MinIO acceptance 的逐项结果
7. 实际运行过的所有测试/检查
8. 明确说明哪些测试没有运行以及原因
9. 是否存在阻塞进入下一阶段的问题
