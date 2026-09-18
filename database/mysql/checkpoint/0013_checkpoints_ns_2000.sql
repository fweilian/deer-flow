ALTER TABLE checkpoints
DROP PRIMARY KEY,
ADD PRIMARY KEY (thread_id, checkpoint_id),
MODIFY COLUMN `checkpoint_ns` VARCHAR(2000) NOT NULL DEFAULT '';
INSERT INTO checkpoint_migrations (v) VALUES (12);
