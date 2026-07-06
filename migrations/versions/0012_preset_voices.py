"""preset_voices catalog (AI Voices) + voice_profiles.sample_duration_seconds

Revision ID: 0012_preset_voices
Revises: 0011_reseed_coin_products
Create Date: 2026-07-06

Каталог пресет-голосов для вкладки Create Cover (ADR-006). `provider_voice` —
внутренний идентификатор голоса fal-модели `fal-ai/elevenlabs/voice-changer`, наружу
не отдаётся.

[RISK-A1] Значения `provider_voice` — реальные имена голосов ElevenLabs, которые
принимает параметр `voice` модели `fal-ai/elevenlabs/voice-changer`. Сверено по
OpenAPI-схеме fal (endpoint_id=fal-ai/elevenlabs/voice-changer) на 2026-07-06:
параметр `voice` — строка (не strict enum) с набором предопределённых голосов
(Aria, Sarah, Laura, Charlotte, Alice, Matilda, Jessica, Lily, George, Callum, Liam,
Brian, ... ; default Rachel). Использованы имена из этого набора. Смена id — data-
миграция без слома клиента (наружу стабильный `key`).

`preview_url` / `sample_duration_seconds` пресетов заполняются отдельной бэкфилл-
миграцией 0013 (эндпоинт терпит NULL).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_preset_voices"
down_revision: str | None = "0011_reseed_coin_products"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (key, title, subtitle, provider_voice, gender, style) — стартовый каталог.
# provider_voice = реальное имя голоса ElevenLabs voice-changer (внутреннее).
PRESET_VOICES = [
    ("aria", "Aria", "Bright pop vocals", "Aria", "female", "pop"),
    ("max", "Max", "Smooth R&B", "Brian", "male", "rnb"),
    ("luna", "Luna", "Dreamy indie", "Charlotte", "female", "indie"),
    ("kai", "Kai", "Hip-hop flow", "Liam", "male", "hip_hop"),
    ("nova", "Nova", "Electronic edge", "Jessica", "female", "electronic"),
    ("leo", "Leo", "Rock energy", "George", "male", "rock"),
    ("sage", "Sage", "Warm acoustic", "Matilda", "female", "acoustic"),
    ("rex", "Rex", "Cinematic depth", "Callum", "male", "cinematic"),
]


def _ts():
    return (
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
    )


def upgrade() -> None:
    op.create_table(
        "preset_voices",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"), primary_key=True,
        ),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("title", sa.String(128), nullable=False),
        sa.Column("subtitle", sa.String(255), nullable=True),
        sa.Column("provider_voice", sa.String(255), nullable=False),
        sa.Column("preview_url", sa.String(1024), nullable=True),
        sa.Column("sample_duration_seconds", sa.Integer(), nullable=True),
        sa.Column("gender", sa.String(16), nullable=True),
        sa.Column("style", sa.String(64), nullable=True),
        sa.Column("language", sa.String(16), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("meta", postgresql.JSONB(), nullable=True),
        *_ts(),
    )
    op.create_index("uq_preset_voices_key", "preset_voices", ["key"], unique=True)
    op.create_index(
        "ix_preset_voices_active_sort", "preset_voices", ["active", "sort_order"]
    )

    # Seed стартового каталога (preview_url / sample_duration_seconds → NULL, бэкфилл в 0013).
    stmt = sa.text(
        "INSERT INTO preset_voices "
        "(key, title, subtitle, provider_voice, gender, style, language, sort_order) "
        "VALUES (:key, :title, :subtitle, :provider_voice, :gender, :style, 'en', :sort_order)"
    )
    bind = op.get_bind()
    for i, (key, title, subtitle, provider_voice, gender, style) in enumerate(PRESET_VOICES):
        bind.execute(
            stmt,
            {
                "key": key,
                "title": title,
                "subtitle": subtitle,
                "provider_voice": provider_voice,
                "gender": gender,
                "style": style,
                "sort_order": i,
            },
        )

    # Длительность образца клона (для ▶️ в My Clones), best-effort в пайплайне.
    op.add_column(
        "voice_profiles",
        sa.Column("sample_duration_seconds", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("voice_profiles", "sample_duration_seconds")
    op.drop_index("ix_preset_voices_active_sort", table_name="preset_voices")
    op.drop_index("uq_preset_voices_key", table_name="preset_voices")
    op.drop_table("preset_voices")
