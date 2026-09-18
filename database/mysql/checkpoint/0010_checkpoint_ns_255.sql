ALTER TABLE checkpoints MODIFY COLUMN `checkpoint_ns` VARCHAR(255) NOT NULL DEFAULT '';
INSERT INTO checkpoint_migrations (v) VALUES (9);
