"""Soft-delete пользовательских ресурсов (ADR-011).

Revision ID: 0015_soft_delete
Revises: 0014_seed_preset_voice_previews
Create Date: 2026-07-08

Добавляет nullable-колонку `deleted_at TIMESTAMPTZ` на `voice_profiles`, `tracks`,
`assets` (soft-delete = UPDATE ... SET deleted_at = now()). Партиал-индексы под
горячие пользовательские листинги ("живые" строки, WHERE deleted_at IS NULL).

Существующие полные индексы (user_id/job_id-пути) НЕ трогаем — они нужны
internal write/finalize и job-путям, работающим с полными строками без фильтра
`deleted_at` (ADR-011 §4, "Миграция 0015").

Данные не переносим: все существующие строки остаются deleted_at = NULL (активны).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_soft_delete"
down_revision: str | None = "0014_seed_preset_voice_previews"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "voice_profiles",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tracks",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "assets",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Партиал-индексы под горячие пользовательские листинги (только "живые" строки).
    op.create_index(
        "ix_tracks_user_created_active",
        "tracks",
        ["user_id", "created_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_assets_user_kind_active",
        "assets",
        ["user_id", "kind", "created_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_voice_profiles_user_active",
        "voice_profiles",
        ["user_id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_voice_profiles_user_active", table_name="voice_profiles")
    op.drop_index("ix_assets_user_kind_active", table_name="assets")
    op.drop_index("ix_tracks_user_created_active", table_name="tracks")
    op.drop_column("assets", "deleted_at")
    op.drop_column("tracks", "deleted_at")
    op.drop_column("voice_profiles", "deleted_at")
