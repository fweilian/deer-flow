# DeerFlow 功能清单

本清单反映 MySQL 重构前的 Goal 0 裁剪结果；历史 PostgreSQL 迁移文件仅供审计，
不是当前运行时能力的描述。

## 保留能力

- 浏览器 Web Chat、SSE 流式响应与 StreamBridge。
- 常规 MCP 工具调用、OAuth 和服务端配置同步。
- 常规 `task` 子智能体委派与受管子智能体定义。
- LangGraph checkpoint（包括 DeltaChannel）与线程/运行事件持久化。
- 定时任务、项目、用户、反馈及个人访问令牌。
- 内存线程元数据使用内部异步字典，不再依赖 LangGraph Store。

## 已移除能力

| 功能 | 状态 | 替代策略 |
| --- | --- | --- |
| IM Channel、连接绑定和 GitHub Webhook | 已删除 | 使用浏览器 Web Chat 或在未来单独设计 MySQL 原生接入。 |
| MCP 长任务轮询、通知和后台任务面板 | 已删除 | 保留普通 MCP 同步工具调用；未来需求须按 MySQL 原生方案重新设计。 |
| 子智能体批处理、队列和批次面板 | 已删除 | 保留普通 `task`；未来批处理须按 MySQL 原生方案重新设计。 |
| LangGraph Store | 已删除 | 线程元数据由专用仓储负责，内存模式使用私有字典。 |

## 当前应用元数据

当前 ORM 共有 12 张应用表、139 个列：`agents`、`feedback`、
`managed_subagents`、`personal_access_tokens`、`projects`、`run_events`、
`runs`、`scheduled_task_runs`、`scheduled_tasks`、`threads_meta`、
`user_preferences`、`users`。

后续 MySQL 实现以这一清单为新的设计边界；不得从历史迁移中恢复已移除表。
