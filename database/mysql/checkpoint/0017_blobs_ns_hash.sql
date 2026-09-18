ALTER TABLE checkpoint_blobs
/*!50700 ADD COLUMN checkpoint_ns_hash BINARY(16) AS (UNHEX(MD5(checkpoint_ns))) STORED,*//*M! ADD COLUMN checkpoint_ns_hash BINARY(16),*/
DROP PRIMARY KEY,
ADD PRIMARY KEY (thread_id, checkpoint_ns_hash, channel, version);
INSERT INTO checkpoint_migrations (v) VALUES (16);
