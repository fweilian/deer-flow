CREATE INDEX checkpoints_thread_id_idx ON checkpoints (thread_id);
INSERT INTO checkpoint_migrations (v) VALUES (5);
