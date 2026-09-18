ALTER TABLE checkpoint_writes MODIFY COLUMN `checkpoint_ns` VARCHAR(255) NOT NULL DEFAULT '';
INSERT INTO checkpoint_migrations (v) VALUES (11);
