# G1 — MySQL Compatibility Gates：验证记录

验证日期：2026-09-18。目标服务：MySQL 8.0.24 / InnoDB。

## V1：独立 Alembic 链

结论：通过。

- PostgreSQL 历史链的当前 head 为 `0023_user_preferences`；历史 revision 未修改。
- `bootstrap._migration_script_location()` 明确按 backend 选择脚本目录：`postgres` / `sqlite` 只使用既有 `migrations/`，`mysql` 只使用预留的 `migrations_mysql/`。
- 已移除模块级 `_HEAD_REVISION` 缓存；head 读取每次针对所选 `script_location` 重新计算，没有按链 cache 或多链框架。
- `backend/tests/test_mysql_migration_chain_gate.py` 在隔离目录中构造仅含 `0001_mysql_baseline` 的 MySQL 根链，并证明：两链各自单 head、MySQL revision 无 `down_revision`、外部 `alembic upgrade head` 使用默认 `alembic_version`，错误 revision 可被检测为与当前 head 不匹配。

这只是 G1 的接线证明；Goal 2 才会写入一次成型的实际 `0001_mysql_baseline`，不会重放 PostgreSQL 的 `0001`–`0023`。

## V5：`langgraph-checkpoint-mysql[asyncmy]==3.0.0`

结论：通过第 1 档（直接依赖 / 正确接入）。不需要 adapter、subclass 或 vendor patch。

真实数据库验证使用 `AsyncMySaver(conn=<asyncmy.Pool>)`，而不是上游的单连接 `from_conn_string()`。测试环境显式调用 `saver.setup()` 创建临时 checkpoint 表；这条测试路径不属于 Production Runtime。

`backend/tests/test_mysql_checkpoint_gate.py` 覆盖：

- `aput`、`aget_tuple`、`alist`（filter / before / limit）、`aput_writes`、`adelete_thread`；
- namespace、parent checkpoint / resume、metadata、pending writes（normal / interrupt / retry channel）、branch/regenerate 所用的 `aget_tuple` + `aput` 复制语义、serde 二进制 round-trip；
- `checkpoint_channel_mode=full` 所需的非 shallow `AsyncMySaver`、并发 checkpoint 写入，以及 asyncmy pool 的多连接借用与关闭；
- 真实 LangGraph / DeerFlow Runtime：`interrupt → checkpoint → Command(resume)`、节点 retry、Gateway `_prepare_regenerate_payload()` 解析历史 checkpoint 并由 LangGraph 从 replay base 生成新回答、Gateway `_branch_thread_with_reservation()` 将 source checkpoint 写入新 thread，以及 DeerFlow `_rollback_to_pre_run_checkpoint()` 均由 `AsyncMySaver(conn=asyncmy.Pool)` 在 MySQL 8.0.24 上执行；中断 checkpoint 的 pending writes 可读，rollback 恢复原 messages 并只重新附加原 task 的 pending write；
- focused pool concurrency：同一 `AsyncMySaver(conn=pool)` 上以 `asyncio.gather()` 并发启动两个独立 LangGraph thread 的 interrupt/resume 流程；两条结果仅含各自 thread 的输入与 resume 值，无 transaction/state contamination 或 event-loop error；
- `LONGBLOB` / 4 GiB schema、`INSERT IGNORE` 所涉 identifier 列均为 `VARCHAR(150)` 且 DeerFlow 实际值在界限内；
- 大 checkpoint：raw blob `1,048,592 B`，JSON/base64 transport 估算 `1,398,124 B`，`max_allowed_packet = 67,108,864 B`，余量 `65,710,740 B`。最终验证样本写入 `59.73 ms`、读取 `11.37 ms`；这是 Gate 证据，不是性能 SLA。

当前生产代码对 `pending_sends` / `Send(...)` 无直接命中，因此没有为未触达的路径新增独立行为测试。

没有实现 checkpoint 对象存储溢出、Redis cache、同步 MySQL Saver 或任何 vendor fork。

## 可重复执行

```bash
cd backend
UV_CACHE_DIR=/tmp/deerflow-uv-cache uv run pytest tests/test_mysql_migration_chain_gate.py -q
TEST_MYSQL_URI='mysql+asyncmy://…/disposable_database' \
  UV_CACHE_DIR=/tmp/deerflow-uv-cache \
  uv run pytest tests/test_mysql_checkpoint_gate.py -q -s
```

第二条命令必须指向可丢弃且拥有 DDL 权限的测试库；Production Runtime 仍不得调用 `setup()`。
