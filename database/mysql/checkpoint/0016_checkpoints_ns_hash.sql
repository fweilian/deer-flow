ALTER TABLE checkpoints
/*!50700 ADD COLUMN checkpoint_ns_hash BINARY(16) AS (UNHEX(MD5(checkpoint_ns))) STORED,*//*M! ADD COLUMN checkpoint_ns_hash BINARY(16),*/
DROP PRIMARY KEY,
ADD PRIMARY KEY (thread_id, checkpoint_ns_hash, checkpoint_id);
INSERT INTO checkpoint_migrations (v) VALUES (15);
