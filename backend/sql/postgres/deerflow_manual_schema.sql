-- Manual PostgreSQL schema for DeerFlow zero-DDL production deployments.
--
-- Usage:
-- 1. Create the target database ahead of time.
-- 2. Run this script with a role that has DDL privileges.
-- 3. Start DeerFlow with:
--      database:
--        backend: postgres
--        postgres_url: $DATABASE_URL
--        schema_init: manual
--
-- This script intentionally does NOT create pgvector-related objects because
-- DeerFlow does not enable vector indexing for the LangGraph store by default.

-- ---------------------------------------------------------------------------
-- LangGraph checkpointer schema
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS checkpoint_migrations (
    v INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS checkpoints (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    parent_checkpoint_id TEXT,
    type TEXT,
    checkpoint JSONB NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);

CREATE TABLE IF NOT EXISTS checkpoint_blobs (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL,
    version TEXT NOT NULL,
    type TEXT NOT NULL,
    blob BYTEA,
    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
);

CREATE TABLE IF NOT EXISTS checkpoint_writes (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    channel TEXT NOT NULL,
    type TEXT,
    blob BYTEA NOT NULL,
    task_path TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);

CREATE INDEX IF NOT EXISTS checkpoints_thread_id_idx
    ON checkpoints (thread_id);

CREATE INDEX IF NOT EXISTS checkpoint_blobs_thread_id_idx
    ON checkpoint_blobs (thread_id);

CREATE INDEX IF NOT EXISTS checkpoint_writes_thread_id_idx
    ON checkpoint_writes (thread_id);

INSERT INTO checkpoint_migrations (v)
VALUES (0), (1), (2), (3), (4), (5), (6), (7), (8), (9)
ON CONFLICT (v) DO NOTHING;

-- ---------------------------------------------------------------------------
-- LangGraph store schema
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS store_migrations (
    v INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS store (
    prefix TEXT NOT NULL,
    key TEXT NOT NULL,
    value JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMPTZ,
    ttl_minutes INT,
    PRIMARY KEY (prefix, key)
);

CREATE INDEX IF NOT EXISTS store_prefix_idx
    ON store USING btree (prefix text_pattern_ops);

CREATE INDEX IF NOT EXISTS idx_store_expires_at
    ON store (expires_at)
    WHERE expires_at IS NOT NULL;

INSERT INTO store_migrations (v)
VALUES (0), (1), (2), (3)
ON CONFLICT (v) DO NOTHING;

-- ---------------------------------------------------------------------------
-- DeerFlow ORM schema
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS threads_meta (
    thread_id VARCHAR(64) PRIMARY KEY,
    assistant_id VARCHAR(128),
    user_id VARCHAR(64),
    display_name VARCHAR(256),
    status VARCHAR(20) NOT NULL DEFAULT 'idle',
    metadata_json JSON NOT NULL DEFAULT '{}'::json,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_threads_meta_assistant_id
    ON threads_meta (assistant_id);

CREATE INDEX IF NOT EXISTS ix_threads_meta_user_id
    ON threads_meta (user_id);


CREATE TABLE IF NOT EXISTS runs (
    run_id VARCHAR(64) PRIMARY KEY,
    thread_id VARCHAR(64) NOT NULL,
    assistant_id VARCHAR(128),
    user_id VARCHAR(64),
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    model_name VARCHAR(128),
    multitask_strategy VARCHAR(20) NOT NULL DEFAULT 'reject',
    metadata_json JSON NOT NULL DEFAULT '{}'::json,
    kwargs_json JSON NOT NULL DEFAULT '{}'::json,
    error TEXT,
    message_count INTEGER NOT NULL DEFAULT 0,
    first_human_message TEXT,
    last_ai_message TEXT,
    total_input_tokens INTEGER NOT NULL DEFAULT 0,
    total_output_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    llm_call_count INTEGER NOT NULL DEFAULT 0,
    lead_agent_tokens INTEGER NOT NULL DEFAULT 0,
    subagent_tokens INTEGER NOT NULL DEFAULT 0,
    middleware_tokens INTEGER NOT NULL DEFAULT 0,
    follow_up_to_run_id VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_runs_thread_id
    ON runs (thread_id);

CREATE INDEX IF NOT EXISTS ix_runs_user_id
    ON runs (user_id);

CREATE INDEX IF NOT EXISTS ix_runs_thread_status
    ON runs (thread_id, status);


CREATE TABLE IF NOT EXISTS run_events (
    id BIGSERIAL PRIMARY KEY,
    thread_id VARCHAR(64) NOT NULL,
    run_id VARCHAR(64) NOT NULL,
    user_id VARCHAR(64),
    event_type VARCHAR(32) NOT NULL,
    category VARCHAR(16) NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    event_metadata JSON NOT NULL DEFAULT '{}'::json,
    seq INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_events_thread_seq UNIQUE (thread_id, seq)
);

CREATE INDEX IF NOT EXISTS ix_run_events_user_id
    ON run_events (user_id);

CREATE INDEX IF NOT EXISTS ix_events_thread_cat_seq
    ON run_events (thread_id, category, seq);

CREATE INDEX IF NOT EXISTS ix_events_run
    ON run_events (thread_id, run_id, seq);


CREATE TABLE IF NOT EXISTS feedback (
    feedback_id VARCHAR(64) PRIMARY KEY,
    run_id VARCHAR(64) NOT NULL,
    thread_id VARCHAR(64) NOT NULL,
    user_id VARCHAR(64),
    message_id VARCHAR(64),
    rating INTEGER NOT NULL,
    comment TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_feedback_thread_run_user UNIQUE (thread_id, run_id, user_id)
);

CREATE INDEX IF NOT EXISTS ix_feedback_run_id
    ON feedback (run_id);

CREATE INDEX IF NOT EXISTS ix_feedback_thread_id
    ON feedback (thread_id);

CREATE INDEX IF NOT EXISTS ix_feedback_user_id
    ON feedback (user_id);


CREATE TABLE IF NOT EXISTS users (
    id VARCHAR(36) PRIMARY KEY,
    email VARCHAR(320) NOT NULL,
    password_hash VARCHAR(128),
    system_role VARCHAR(16) NOT NULL DEFAULT 'user',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    oauth_provider VARCHAR(32),
    oauth_id VARCHAR(128),
    needs_setup BOOLEAN NOT NULL DEFAULT FALSE,
    token_version INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email
    ON users (email);

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_oauth_identity
    ON users (oauth_provider, oauth_id)
    WHERE oauth_provider IS NOT NULL AND oauth_id IS NOT NULL;
