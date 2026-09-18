ALTER TABLE checkpoints MODIFY COLUMN checkpoint_ns_hash BINARY(16);
INSERT INTO checkpoint_migrations (v) VALUES (19);
