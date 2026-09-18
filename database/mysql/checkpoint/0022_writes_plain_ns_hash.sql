ALTER TABLE checkpoint_writes MODIFY COLUMN checkpoint_ns_hash BINARY(16);
INSERT INTO checkpoint_migrations (v) VALUES (21);
