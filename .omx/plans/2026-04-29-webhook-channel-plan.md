# Webhook Channel（GitHub/Gitee）实施计划

## Requirements Summary

1. 在 `backend/app/channels` 下新增 `webhook` channel，实现为 DeerFlow 原生 channel，而不是独立于 channel 体系的旁路服务。
   - 需要遵循 `Channel` 抽象（`start/stop/send`）与 bus 收发机制：`backend/app/channels/base.py:14`、`backend/app/channels/base.py:36`、`backend/app/channels/base.py:46`、`backend/app/channels/base.py:87`。
2. webhook 入口需接收 GitHub 与 Gitee 事件，支持事件过滤、签名校验、幂等去重、速率限制、最大 body 控制；处理逻辑参考 `temp/webhook.txt`。
   - 参考能力点：路由级配置与校验 `temp/webhook.txt:8`、`temp/webhook.txt:119`；body/签名/限流 `temp/webhook.txt:308`、`temp/webhook.txt:323`、`temp/webhook.txt:334`；幂等 `temp/webhook.txt:420`。
3. webhook 入站消息需可选择：
   - 走 LLM（转 InboundMessage 进入 `ChannelManager`）
   - 或 `deliver_only` 直发到目标 channel 用户。
   - 参考 `deliver_only` 分支：`temp/webhook.txt:439`。
4. webhook 的响应目标不是固定回 `webhook` 自身，而是可路由到“其他 channel 的某个用户”；当未配置目标时默认仅打印日志（`deliver=log`）。
   - 当前 `ChannelManager` 出站默认回源 channel：`backend/app/channels/manager.py:755`、`backend/app/channels/manager.py:816`、`backend/app/channels/manager.py:858`、`backend/app/channels/manager.py:920`、`backend/app/channels/manager.py:953`。
5. channel 注册、生命周期与配置读取需接入现有 `ChannelService`。
   - 注册点：`backend/app/channels/service.py:17`。
   - 生命周期：`backend/app/channels/service.py:80`、`backend/app/channels/service.py:129`。

## Scope And Assumptions

1. 本轮限定支持 GitHub/Gitee webhook，不扩展 GitLab/Stripe 等其他源。
2. webhook 入站接口复用现有 gateway 进程（FastAPI）承载，channel 内部负责业务处理，避免额外监听端口与额外依赖。
   - 当前 gateway 已统一挂载 router：`backend/app/gateway/app.py:210`。
3. “发送到其他 channel 用户”依赖目标 channel 已启用且可按 `chat_id` 发消息（如 telegram 用户 id、wecom user_id）。
4. Gitee 签名按可配置模式实现（plain token / HMAC），默认兼容 `X-Gitee-Token` 头。

## Acceptance Criteria

1. 当 `channels.webhook.enabled=true` 时，系统可接收 `POST /api/channels/webhook/{route_name}`，并返回：
   - `202 accepted`（异步走 LLM）
   - `200 delivered`（deliver_only）
   - `401/404/413/429`（校验失败/路由不存在/body 超限/限流）
2. 可基于 route 配置接收 GitHub 与 Gitee 事件，按事件名过滤（例如只收 `push`）。
3. 可对同一 delivery 去重（同一 delivery_id 重试不重复触发）。
4. 能把处理结果投递到指定目标 channel/chat（不回 `webhook` channel）；未配置目标时仅打印日志。
5. 在 LLM 模式下，能将模板化 prompt 投递到 `ChannelManager` 并正常回包到目标用户；若无 `reply_target` 则回包为日志输出。
6. 单元测试覆盖关键路径：签名、过滤、幂等、target 路由、deliver_only。

## Implementation Steps

1. 新增 `WebhookChannel` 与配置模型解析
   - 新建 `backend/app/channels/webhook.py`，实现 `Channel` 子类，职责包含：
     - route 配置加载/校验（`events/secret/prompt/deliver/deliver_extra/deliver_only`）
     - webhook 请求处理入口方法（供 router 调用）
   - `send()` 中根据目标 channel 转发；默认行为为仅打印日志（`deliver=log`）
   - 将 `webhook` 加入 registry：`backend/app/channels/service.py:17`。

2. 增加 gateway webhook 接口并桥接到 channel
   - 在 `backend/app/gateway/routers/channels.py` 增加：
     - `POST /api/channels/webhook/{route_name}`
     - `GET /api/channels/webhook/health`（可选）
   - 路由处理只做 transport 层（读取 raw body + headers + query），业务交给 `WebhookChannel.handle_webhook_request(...)`。
   - 若 `webhook` channel 未启用，返回 `503`。

3. 在 `WebhookChannel` 中实现参考 `temp/webhook.txt` 的安全与流控能力
   - 最大 body 限制：`temp/webhook.txt:308`。
   - 签名校验（GitHub + Gitee）：参考 `temp/webhook.txt:559` 的多头策略。
   - 事件过滤：`temp/webhook.txt:360`。
   - 速率限制与幂等缓存：`temp/webhook.txt:334`、`temp/webhook.txt:420`。
   - 统一响应结构（accepted/ignored/duplicate/error）。

4. 实现 prompt 渲染与模式分流（LLM / deliver_only）
   - 模板渲染支持 `{a.b.c}` 与 `{__raw__}`（参考 `temp/webhook.txt:594`）。
   - `deliver_only=true` 时直接构造 `OutboundMessage` 发往目标 channel，不进入 manager。
   - 非 `deliver_only` 时构造 `InboundMessage`：
     - `channel_name="webhook"`
     - `chat_id` 使用 `webhook:{route}:{delivery_id}`（避免并发互相覆盖，参考 `temp/webhook.txt:497`）
     - `metadata` 写入 `reply_target`、`event_type`、`raw_payload`。

5. 改造 `ChannelManager` 支持“回包目标重定向”
   - 新增 helper（如 `_resolve_outbound_target(msg)`），优先读取 `msg.metadata.reply_target`；当缺失时走默认日志目标（`deliver=log`），不转发至其他 channel。
   - 在以下出站构造点统一使用该 helper：
     - 普通 chat 回包 `backend/app/channels/manager.py:755`
     - streaming interim/final `backend/app/channels/manager.py:816`、`backend/app/channels/manager.py:858`
     - command 回包 `backend/app/channels/manager.py:920`
     - error 回包 `backend/app/channels/manager.py:953`
   - 保持 thread store key 仍使用入站 webhook 会话键，不污染目标 channel 的 thread 映射。

6. 增加配置文档与示例
   - 更新 `config.example.yaml` 与 `README_zh.md`/`README.md` 的 channels 章节，新增 `channels.webhook` 示例：
     - `enabled`
     - `max_body_bytes` / `rate_limit_per_minute`
     - `routes.github_push` / `routes.gitee_pr`
     - `deliver` + `deliver_extra.chat_id`
     - `deliver_only`

7. 测试与回归
   - 新增 `backend/tests/test_webhook_channel.py`，覆盖：
     - route 校验失败（缺 secret、非法 deliver 配置）
     - GitHub/Gitee 签名通过/失败
     - 事件过滤/幂等去重/限流
     - deliver_only 直发到目标 channel
     - LLM 流程下 metadata.reply_target 被 manager 正确使用
     - 未配置 `reply_target` 时仅记录日志、不产生跨 channel 发送
   - 在 `backend/tests/test_channels.py` 增加 manager target override 的用例。

8. 本地验证脚本（手工冒烟）
   - 启用 `channels.webhook` + `channels.telegram`（或 wecom）。
   - `curl` 发送 GitHub/Gitee 样例 payload（含签名头），观察：
     - API 返回码
     - manager 日志
     - 目标 IM 用户收到消息

## Risks And Mitigations

1. 风险：`ChannelManager` 回包重定向改动影响现有 channel 行为。
   - 缓解：override 仅在 `metadata.reply_target` 存在时生效，默认路径不变；补充回归单测。
2. 风险：Gitee 签名存在“token 明文/签名模式”差异。
   - 缓解：配置化 `signature_mode`，并在日志中打印判定分支（不打印 secret）。
3. 风险：deliver_only 与 LLM 模式配置混乱导致误用。
   - 缓解：启动时做 route 配置校验；`deliver_only=true` 时强制要求合法 `deliver` 目标（参考 `temp/webhook.txt:129`）。
4. 风险：未配置回包目标时被误认为“发送失败”。
   - 缓解：默认策略明确为 `deliver=log`，并在响应与日志中标注 `target=log`。
5. 风险：幂等缓存内存增长。
   - 缓解：按 TTL 周期清理（参考 `temp/webhook.txt:423`）并允许配置 TTL。

## Verification Steps

1. 单测
   - `cd /home/fweil/gitprojects/deer-flow/backend && uv run pytest tests/test_webhook_channel.py tests/test_channels.py`
2. 代码检查
   - `cd /home/fweil/gitprojects/deer-flow/backend && uv run ruff check app/channels app/gateway/routers tests`
3. webhook 冒烟（GitHub）
   - 使用固定 payload + secret 生成 `X-Hub-Signature-256`，调用 `POST /api/channels/webhook/github_push`，验证 `202/200` 与目标 channel 收件。
4. webhook 冒烟（Gitee）
   - 使用 `X-Gitee-Token`（按配置模式）调用 `POST /api/channels/webhook/gitee_push`，验证事件过滤与回包路由。

## Out Of Scope

1. 不实现动态 route 文件热加载（`temp/webhook.txt:262` 的 dynamic routes 机制）。
2. 不做跨进程共享幂等缓存（本轮仅进程内 TTL）。
3. 不扩展 GitHub PR comment 回写等平台 API 反向操作（本轮聚焦“接收 webhook -> 发送到 IM 用户”）。
