# MySQL application migration chain

This is an independent Alembic `script_location` for the MySQL fresh-cutover
chain. It is the sole active application migration tree.

The DBA/migrator invokes it outside Gateway, for example:

```bash
cd backend
PYTHONPATH=.:packages/harness python scripts/migrate_mysql.py \
  --url 'mysql://migrator:…@db:3306/deerflow'
```

The script deliberately maps the runtime `mysql+asyncmy` URL to the migration
process's `mysql+pymysql` URL. Gateway never calls this script, Alembic
`upgrade`, `stamp`, or `create_all`.

`0001_mysql_baseline` is the frozen fresh-cutover root. It creates exactly the
12 application tables and their MySQL-specific correctness keys. Checkpoint
tables remain exclusively owned by `database/mysql/checkpoint/`.

The baseline is intentionally immutable: a later MySQL business-schema change
must be a new `0002+` revision, never an edit to `0001_mysql_baseline`.
