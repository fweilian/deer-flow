CREATE INDEX checkpoints_checkpoint_id_idx ON checkpoints (checkpoint_id);
INSERT INTO checkpoint_migrations (v) VALUES (8);
