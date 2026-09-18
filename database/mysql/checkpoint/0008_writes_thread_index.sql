CREATE INDEX checkpoint_writes_thread_id_idx ON checkpoint_writes (thread_id);
INSERT INTO checkpoint_migrations (v) VALUES (7);
