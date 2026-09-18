CREATE INDEX checkpoint_blobs_thread_id_idx ON checkpoint_blobs (thread_id);
INSERT INTO checkpoint_migrations (v) VALUES (6);
