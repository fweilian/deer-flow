ALTER TABLE checkpoint_blobs MODIFY COLUMN `checkpoint_ns` VARCHAR(255) NOT NULL DEFAULT '';
INSERT INTO checkpoint_migrations (v) VALUES (10);
