# MySQL checkpoint schema migrations

These 22 ordered migrations are mechanically derived from
`langgraph-checkpoint-mysql[asyncmy]==3.0.0` (`base.MIGRATIONS`). They are
owned by the DBA/migration process, never by the Gateway runtime.

`SAVER_VERSION = 3.0.0`  
`MIGRATIONS_COUNT = 22`

Use the DBA-only runner (with `PyMySQL` and the pinned saver installed):

```bash
python database/mysql/checkpoint/apply.py --url 'mysql://migrator:…@db:3306/deerflow'
```

It executes the files in lexical order and records each zero-based migration
number in `checkpoint_migrations`; it skips completed versions and refuses
gaps. The files are a linear, one-time evolution chain: they are not
individually idempotent, and repeated DDL such as index creation will fail.
`verify.sql` is read-only and must report all four Saver-owned tables and
version 21 before deploying a runtime using this Saver version.

The application Alembic chain does not own `checkpoint_migrations`,
`checkpoints`, `checkpoint_blobs`, or `checkpoint_writes`. When upgrading the
Saver, review its `MIGRATIONS`, regenerate this artifact, run the new DBA
migrations, then deploy the matching runtime.
