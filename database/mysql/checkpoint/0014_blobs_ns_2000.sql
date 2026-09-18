ALTER TABLE checkpoint_blobs
DROP PRIMARY KEY,
ADD PRIMARY KEY (thread_id, channel, version),
MODIFY COLUMN `checkpoint_ns` VARCHAR(2000) NOT NULL DEFAULT '';
INSERT INTO checkpoint_migrations (v) VALUES (13);
