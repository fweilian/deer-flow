"""add cron scheduler tables

Revision ID: 20260506_01
Revises:
Create Date: 2026-05-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260506_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cron_jobs",
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column("assistant_id", sa.String(length=128), nullable=True),
        sa.Column("cron_expr", sa.String(length=128), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("input_json", sa.JSON(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("context_json", sa.JSON(), nullable=False),
        sa.Column("multitask_strategy", sa.String(length=20), nullable=False, server_default="enqueue"),
        sa.Column("next_fire_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_fire_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_id", sa.String(length=64), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("job_id"),
    )
    op.create_index(op.f("ix_cron_jobs_thread_id"), "cron_jobs", ["thread_id"], unique=False)
    op.create_index(op.f("ix_cron_jobs_next_fire_at"), "cron_jobs", ["next_fire_at"], unique=False)

    op.create_table(
        "cron_job_fires",
        sa.Column("fire_id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("scheduled_fire_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="claimed"),
        sa.Column("claim_owner", sa.String(length=128), nullable=True),
        sa.Column("claim_token", sa.String(length=128), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["cron_jobs.job_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("fire_id"),
        sa.UniqueConstraint("job_id", "scheduled_fire_at", name="uq_cron_job_fires_job_sched"),
    )
    op.create_index(op.f("ix_cron_job_fires_job_id"), "cron_job_fires", ["job_id"], unique=False)
    op.create_index(op.f("ix_cron_job_fires_scheduled_fire_at"), "cron_job_fires", ["scheduled_fire_at"], unique=False)
    op.create_index(op.f("ix_cron_job_fires_lease_until"), "cron_job_fires", ["lease_until"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_cron_job_fires_lease_until"), table_name="cron_job_fires")
    op.drop_index(op.f("ix_cron_job_fires_scheduled_fire_at"), table_name="cron_job_fires")
    op.drop_index(op.f("ix_cron_job_fires_job_id"), table_name="cron_job_fires")
    op.drop_table("cron_job_fires")
    op.drop_index(op.f("ix_cron_jobs_next_fire_at"), table_name="cron_jobs")
    op.drop_index(op.f("ix_cron_jobs_thread_id"), table_name="cron_jobs")
    op.drop_table("cron_jobs")
