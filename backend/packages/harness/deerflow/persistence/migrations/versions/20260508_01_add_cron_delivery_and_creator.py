"""Add creator and delivery metadata to cron jobs.

Revision ID: 20260508_01
Revises: 20260506_01
Create Date: 2026-05-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260508_01"
down_revision = "20260506_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("cron_jobs") as batch_op:
        batch_op.add_column(sa.Column("creator_user_id", sa.String(length=64), nullable=False, server_default="default"))
        batch_op.add_column(sa.Column("delivery_json", sa.JSON(), nullable=True))
        batch_op.create_index(batch_op.f("ix_cron_jobs_creator_user_id"), ["creator_user_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("cron_jobs") as batch_op:
        batch_op.drop_index(batch_op.f("ix_cron_jobs_creator_user_id"))
        batch_op.drop_column("delivery_json")
        batch_op.drop_column("creator_user_id")
