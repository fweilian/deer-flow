ALTER TABLE checkpoint_writes ADD COLUMN task_path VARCHAR(2000) NOT NULL DEFAULT '';
INSERT INTO checkpoint_migrations (v) VALUES (18);
