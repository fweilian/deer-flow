# MySQL Goal 0 审计记录

日期：2026-09-18

**状态：DONE** —— 产出提交 `a55e5734`
（`refactor(runtime): remove channels, background MCP tasks, subagent batches, and LangGraph Store`，
分支 `feat_portal`，工作区干净）。

## 运行时依赖扫描

已对 `backend`、`frontend` 和 `config.example.yaml` 执行生产代码扫描，排除不可变的
`backend/packages/harness/deerflow/persistence/migrations/versions/` 历史迁移及测试、
文档。扫描词包括已删除的 Channel、MCP 长任务、子智能体批处理、Webhook 和
`runtime.store` 导入路径。结果为零个生产引用。

历史 PostgreSQL/Alembic 迁移链被原样保留用于审计，不由 Gateway 回放；启动器只会从
当前 ORM 元数据创建空库，并会拒绝现有旧版架构。后续迁移必须经 MySQL 的全新建表与
数据导入流程完成。

## 当前元数据

ORM 反射结果：12 张应用表、139 个列。

`agents`, `feedback`, `managed_subagents`, `personal_access_tokens`, `projects`,
`run_events`, `runs`, `scheduled_task_runs`, `scheduled_tasks`, `threads_meta`,
`user_preferences`, `users`。

已删除功能的持久化表不在当前 ORM 元数据中。
