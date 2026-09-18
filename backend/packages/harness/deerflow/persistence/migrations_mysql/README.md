# MySQL application migration chain

This is an independent Alembic `script_location` for the MySQL fresh-cutover
chain. It never reads or replays the immutable PostgreSQL history under
`persistence/migrations/`.

The DBA/migrator invokes it outside Gateway, for example:

```bash
cd backend
PYTHONPATH=.:packages/harness python scripts/migrate_mysql.py \
  --url 'mysql://migrator:…@db:3306/deerflow'
```

The script deliberately maps the runtime `mysql+asyncmy` URL to the migration
process's `mysql+pymysql` URL. Gateway never calls this script, Alembic
`upgrade`, `stamp`, or `create_all`.

`0001_mysql_baseline` is intentionally not present yet: Goal 2 establishes
this isolated infrastructure and its verification primitive; the final
business-schema baseline is accepted later by the migration plan.
