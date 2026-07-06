"""add video stages to job_stage enum (visual_gen, mux_audio, lyrics_render)

Revision ID: 0013_video_stages
Revises: 0012_preset_voices
Create Date: 2026-07-06

Feature B (ADR-007): 3 режима видео добавляют новые стадии пайплайна. `job_stage` —
нативный PG enum (0002), поэтому новые значения добавляются через ALTER TYPE ADD VALUE.

PostgreSQL (PG12+) не позволяет использовать новое значение enum в той же транзакции, где
оно добавлено; ADD VALUE выполняется в autocommit_block (образец 0010). `AssetKind.image`
уже присутствует в enum `asset_kind` с 0002 — отдельный ADD VALUE не требуется.

`VideoStyle` / `VideoAspect` / `VideoMode` хранятся строками в `Asset.meta` — отдельного
PG-типа и миграции не требуют.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013_video_stages"
down_revision: str | None = "0012_preset_voices"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_STAGES = ("visual_gen", "mux_audio", "lyrics_render")


def upgrade() -> None:
    # autocommit_block: каждое ADD VALUE коммитится немедленно, вне общей транзакции миграций.
    with op.get_context().autocommit_block():
        for value in _NEW_STAGES:
            op.execute(f"ALTER TYPE job_stage ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # PostgreSQL не поддерживает удаление значения enum — no-op (задокументировано, образец 0010).
    pass
