"""voice: voice_consents, voice_profiles

Revision ID: 0006_voice
Revises: 0005_seed_products
Create Date: 2026-06-18

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_voice"
down_revision: str | None = "0005_seed_products"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VOICE_CONSENT_KIND = ("own_voice", "third_party_authorized")
VOICE_PROFILE_STATUS = ("pending", "ready", "failed")


def _ts():
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )


def upgrade() -> None:
    bind = op.get_bind()
    consent_kind = postgresql.ENUM(*VOICE_CONSENT_KIND, name="voice_consent_kind", create_type=False)
    profile_status = postgresql.ENUM(*VOICE_PROFILE_STATUS, name="voice_profile_status", create_type=False)
    consent_kind.create(bind, checkfirst=True)
    profile_status.create(bind, checkfirst=True)

    op.create_table(
        "voice_consents",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", consent_kind, nullable=False),
        sa.Column("accepted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("statement", sa.Text(), nullable=True),
        *_ts(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_voice_consents_user_id_users", ondelete="CASCADE"),
    )
    op.create_index("ix_voice_consents_user_id", "voice_consents", ["user_id"])

    op.create_table(
        "voice_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(120), nullable=True),
        sa.Column("provider_voice_id", sa.String(255), nullable=True),
        sa.Column("status", profile_status, server_default=sa.text("'pending'"), nullable=False),
        sa.Column("consent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("sample_asset_url", sa.String(1024), nullable=True),
        sa.Column("meta", postgresql.JSONB(), nullable=True),
        *_ts(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_voice_profiles_user_id_users", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["consent_id"], ["voice_consents.id"], name="fk_voice_profiles_consent_id_voice_consents", ondelete="SET NULL"),
    )
    op.create_index("ix_voice_profiles_user_id", "voice_profiles", ["user_id"])


def downgrade() -> None:
    op.drop_table("voice_profiles")
    op.drop_table("voice_consents")
    for name in ("voice_profile_status", "voice_consent_kind"):
        postgresql.ENUM(name=name).drop(op.get_bind(), checkfirst=True)
