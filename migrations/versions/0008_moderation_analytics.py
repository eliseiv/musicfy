"""moderation + analytics: moderation_cases, usage_events

Revision ID: 0008_moderation_analytics
Revises: 0007_video_push
Create Date: 2026-06-18

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_moderation_analytics"
down_revision: str | None = "0007_video_push"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MODERATION_STATUS = ("pending", "approved", "blocked", "needs_review")


def upgrade() -> None:
    bind = op.get_bind()
    moderation_status = postgresql.ENUM(
        *MODERATION_STATUS, name="moderation_status", create_type=False
    )
    moderation_status.create(bind, checkfirst=True)

    op.create_table(
        "moderation_cases",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", moderation_status, nullable=False),
        sa.Column("reason", sa.String(255), nullable=True),
        sa.Column("content_excerpt", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_moderation_cases_user_id_users", ondelete="SET NULL"),
    )
    op.create_index("ix_moderation_cases_user_id_created_at", "moderation_cases", ["user_id", "created_at"])
    op.create_index("ix_moderation_cases_status", "moderation_cases", ["status"])

    op.create_table(
        "usage_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("properties", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_usage_events_user_id_users", ondelete="SET NULL"),
    )
    op.create_index("ix_usage_events_name_created_at", "usage_events", ["name", "created_at"])
    op.create_index("ix_usage_events_user_id_created_at", "usage_events", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_table("usage_events")
    op.drop_table("moderation_cases")
    postgresql.ENUM(name="moderation_status").drop(op.get_bind(), checkfirst=True)
