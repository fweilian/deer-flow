"""Final MySQL fresh-cutover application schema.

Revision ID: 0001_mysql_baseline
Revises:

This is a deliberately self-contained, immutable MySQL 8.0.24 baseline.
Checkpoint tables are owned by the separate ``database/mysql/checkpoint``
artifact and must never be added here.
"""

from alembic import op

revision = "0001_mysql_baseline"
down_revision = None
branch_labels = None
depends_on = None


_DDL = (
    """CREATE TABLE agents (
        id VARCHAR(64) NOT NULL, user_id VARCHAR(64) NOT NULL,
        name VARCHAR(128) NOT NULL, config JSON NOT NULL, soul TEXT NOT NULL,
        created_at DATETIME(6) NOT NULL, updated_at DATETIME(6) NOT NULL,
        PRIMARY KEY (id), CONSTRAINT uq_agents_user_name UNIQUE (user_id, name)
    )""",
    "CREATE INDEX ix_agents_user_id ON agents (user_id)",
    """CREATE TABLE feedback (
        feedback_id VARCHAR(64) NOT NULL, run_id VARCHAR(64) NOT NULL,
        thread_id VARCHAR(64) NOT NULL, user_id VARCHAR(64), message_id VARCHAR(64),
        rating INTEGER NOT NULL, comment TEXT, created_at DATETIME(6) NOT NULL,
        PRIMARY KEY (feedback_id),
        CONSTRAINT uq_feedback_thread_run_user UNIQUE (thread_id, run_id, user_id)
    )""",
    "CREATE INDEX ix_feedback_run_id ON feedback (run_id)",
    "CREATE INDEX ix_feedback_thread_id ON feedback (thread_id)",
    "CREATE INDEX ix_feedback_user_id ON feedback (user_id)",
    """CREATE TABLE managed_subagents (
        id VARCHAR(64) NOT NULL, name VARCHAR(128) NOT NULL, definition JSON NOT NULL,
        created_at DATETIME(6) NOT NULL, updated_at DATETIME(6) NOT NULL,
        PRIMARY KEY (id), UNIQUE (name)
    )""",
    """CREATE TABLE personal_access_tokens (
        id VARCHAR(64) NOT NULL, user_id VARCHAR(64) NOT NULL, name VARCHAR(128) NOT NULL,
        token_digest VARCHAR(64) NOT NULL, scopes JSON NOT NULL, expires_at DATETIME(6),
        last_used_at DATETIME(6), created_at DATETIME(6) NOT NULL, revoked_at DATETIME(6),
        PRIMARY KEY (id)
    )""",
    "CREATE UNIQUE INDEX ix_personal_access_tokens_token_digest ON personal_access_tokens (token_digest)",
    "CREATE INDEX ix_personal_access_tokens_user_id ON personal_access_tokens (user_id)",
    """CREATE TABLE projects (
        id VARCHAR(64) NOT NULL, user_id VARCHAR(64) NOT NULL, name VARCHAR(128) NOT NULL,
        instructions TEXT NOT NULL, presentation JSON NOT NULL, status VARCHAR(16) NOT NULL,
        created_at DATETIME(6) NOT NULL, updated_at DATETIME(6) NOT NULL, PRIMARY KEY (id)
    )""",
    "CREATE INDEX ix_projects_status ON projects (status)",
    "CREATE INDEX ix_projects_user_id ON projects (user_id)",
    """CREATE TABLE run_events (
        id INTEGER NOT NULL AUTO_INCREMENT, thread_id VARCHAR(64) NOT NULL,
        run_id VARCHAR(64) NOT NULL, user_id VARCHAR(64), event_type VARCHAR(32) NOT NULL,
        category VARCHAR(16) NOT NULL, content TEXT NOT NULL, event_metadata JSON NOT NULL,
        seq INTEGER NOT NULL, created_at DATETIME(6) NOT NULL, PRIMARY KEY (id),
        CONSTRAINT uq_events_thread_seq UNIQUE (thread_id, seq)
    )""",
    "CREATE INDEX ix_events_run ON run_events (thread_id, run_id, seq)",
    "CREATE INDEX ix_events_thread_cat_seq ON run_events (thread_id, category, seq)",
    "CREATE INDEX ix_run_events_user_id ON run_events (user_id)",
    """CREATE TABLE runs (
        run_id VARCHAR(64) NOT NULL, thread_id VARCHAR(64) NOT NULL,
        assistant_id VARCHAR(128), user_id VARCHAR(64), status VARCHAR(20) NOT NULL,
        operation_kind VARCHAR(32) NOT NULL DEFAULT 'run', idempotency_key VARCHAR(255),
        model_name VARCHAR(128), multitask_strategy VARCHAR(20) NOT NULL,
        metadata_json JSON NOT NULL, kwargs_json JSON NOT NULL, error TEXT, stop_reason VARCHAR(50),
        message_count INTEGER NOT NULL, first_human_message TEXT, last_ai_message TEXT,
        total_input_tokens INTEGER NOT NULL, total_output_tokens INTEGER NOT NULL,
        total_tokens INTEGER NOT NULL, llm_call_count INTEGER NOT NULL,
        lead_agent_tokens INTEGER NOT NULL, subagent_tokens INTEGER NOT NULL,
        middleware_tokens INTEGER NOT NULL,
        token_usage_by_model JSON NOT NULL DEFAULT ('{}'), follow_up_to_run_id VARCHAR(64),
        owner_worker_id VARCHAR(128), lease_expires_at DATETIME(6), cancel_action VARCHAR(20),
        cancel_requested_at DATETIME(6), created_at DATETIME(6) NOT NULL,
        updated_at DATETIME(6) NOT NULL,
        active_thread_id VARCHAR(64) GENERATED ALWAYS AS
          (IF(status IN ('pending', 'running'), thread_id, NULL)) STORED,
        PRIMARY KEY (run_id), UNIQUE KEY uq_runs_thread_active (active_thread_id)
    )""",
    "CREATE INDEX ix_runs_lease ON runs (lease_expires_at)",
    "CREATE INDEX ix_runs_thread_id ON runs (thread_id)",
    "CREATE INDEX ix_runs_thread_status ON runs (thread_id, status)",
    "CREATE INDEX ix_runs_user_id ON runs (user_id)",
    "CREATE UNIQUE INDEX uq_runs_idempotency_key ON runs (idempotency_key)",
    """CREATE TABLE scheduled_task_runs (
        id VARCHAR(64) NOT NULL, task_id VARCHAR(64) NOT NULL, occurrence_seq BIGINT,
        launch_accounted BOOL, thread_id VARCHAR(64) NOT NULL, run_id VARCHAR(64),
        scheduled_for DATETIME(6) NOT NULL, `trigger` VARCHAR(16) NOT NULL,
        status VARCHAR(16) NOT NULL, error TEXT, lease_owner VARCHAR(128),
        lease_expires_at DATETIME(6), attempt_count INTEGER NOT NULL DEFAULT '0',
        started_at DATETIME(6), finished_at DATETIME(6), created_at DATETIME(6) NOT NULL,
        active_task_id VARCHAR(64) GENERATED ALWAYS AS
          (IF(status IN ('queued', 'launching', 'running'), task_id, NULL)) STORED,
        PRIMARY KEY (id), UNIQUE KEY uq_scheduled_task_run_active (active_task_id)
    )""",
    "CREATE INDEX idx_scheduled_task_runs_status_created ON scheduled_task_runs (status, attempt_count, created_at, id)",
    "CREATE INDEX ix_scheduled_task_runs_status ON scheduled_task_runs (status)",
    "CREATE INDEX ix_scheduled_task_runs_task_id ON scheduled_task_runs (task_id)",
    "CREATE INDEX ix_scheduled_task_runs_thread_id ON scheduled_task_runs (thread_id)",
    "CREATE UNIQUE INDEX uq_scheduled_task_run_occurrence_seq ON scheduled_task_runs (task_id, occurrence_seq)",
    """CREATE TABLE scheduled_tasks (
        id VARCHAR(64) NOT NULL, user_id VARCHAR(64) NOT NULL, thread_id VARCHAR(64),
        context_mode VARCHAR(32) NOT NULL, assistant_id VARCHAR(128), title VARCHAR(255) NOT NULL,
        prompt TEXT NOT NULL, schedule_type VARCHAR(16) NOT NULL, schedule_spec JSON NOT NULL,
        timezone VARCHAR(64) NOT NULL, status VARCHAR(16) NOT NULL,
        overlap_policy VARCHAR(16) NOT NULL, next_run_at DATETIME(6), last_run_at DATETIME(6),
        last_run_id VARCHAR(64), last_thread_id VARCHAR(64), last_error TEXT,
        lease_owner VARCHAR(128), lease_expires_at DATETIME(6), run_count INTEGER NOT NULL,
        last_occurrence_seq BIGINT NOT NULL DEFAULT '0', created_at DATETIME(6) NOT NULL,
        updated_at DATETIME(6) NOT NULL, PRIMARY KEY (id)
    )""",
    "CREATE INDEX ix_scheduled_tasks_next_run_at ON scheduled_tasks (next_run_at)",
    "CREATE INDEX ix_scheduled_tasks_status ON scheduled_tasks (status)",
    "CREATE INDEX ix_scheduled_tasks_thread_id ON scheduled_tasks (thread_id)",
    "CREATE INDEX ix_scheduled_tasks_user_id ON scheduled_tasks (user_id)",
    """CREATE TABLE threads_meta (
        thread_id VARCHAR(64) NOT NULL, incarnation VARCHAR(32), assistant_id VARCHAR(128),
        user_id VARCHAR(64), project_id VARCHAR(64), display_name VARCHAR(256),
        status VARCHAR(20) NOT NULL, metadata_json JSON NOT NULL,
        created_at DATETIME(6) NOT NULL, updated_at DATETIME(6) NOT NULL, PRIMARY KEY (thread_id)
    )""",
    "CREATE INDEX ix_threads_meta_assistant_id ON threads_meta (assistant_id)",
    "CREATE INDEX ix_threads_meta_project_id ON threads_meta (project_id)",
    "CREATE INDEX ix_threads_meta_user_id ON threads_meta (user_id)",
    """CREATE TABLE users (
        id VARCHAR(36) NOT NULL, email VARCHAR(320) NOT NULL, password_hash VARCHAR(128),
        system_role VARCHAR(16) NOT NULL, created_at DATETIME(6) NOT NULL,
        oauth_provider VARCHAR(32), oauth_id VARCHAR(128), needs_setup BOOL NOT NULL,
        token_version INTEGER NOT NULL, PRIMARY KEY (id)
    )""",
    "CREATE UNIQUE INDEX idx_users_oauth_identity ON users (oauth_provider, oauth_id)",
    "CREATE UNIQUE INDEX ix_users_email ON users (email)",
    """CREATE TABLE user_preferences (
        user_id VARCHAR(36) NOT NULL, `key` VARCHAR(40) NOT NULL, value JSON,
        PRIMARY KEY (user_id, `key`),
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )""",
)


def upgrade() -> None:
    for statement in _DDL:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("MySQL fresh-cutover baseline has no in-place downgrade path")
