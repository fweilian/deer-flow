ALTER TABLE checkpoint_blobs MODIFY COLUMN `blob` LONGBLOB;
INSERT INTO checkpoint_migrations (v) VALUES (4);
