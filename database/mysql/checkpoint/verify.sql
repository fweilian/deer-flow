SELECT table_name
FROM information_schema.tables
WHERE table_schema = DATABASE()
  AND table_name IN ('checkpoint_migrations', 'checkpoints', 'checkpoint_blobs', 'checkpoint_writes')
ORDER BY table_name;

SELECT MAX(v) AS checkpoint_migration_version
FROM checkpoint_migrations;
