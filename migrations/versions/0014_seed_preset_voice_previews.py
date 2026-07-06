"""Backfill preset_voices.preview_url + sample_duration_seconds (закрытие TD-006).

Revision ID: 0014_seed_preset_voice_previews
Revises: 0013_video_stages
Create Date: 2026-07-06

Бэкфилл превью-сэмплов для 8 пресет-голосов (ADR-006). До этой миграции
`preview_url` / `sample_duration_seconds` были NULL (сидинг 0012 их не заполнял,
эндпоинт GET /v1/presets/voices терпит NULL).

Как получены URL (реальный fal, 2026-07-06):
  1. Эталонный вокал-клип сгенерирован через FAL_SPEECH_MODEL
     (fal-ai/minimax/speech-02-turbo), текст:
     "This is a preview of my voice on Musicfy. Let's make some music together."
     Результат: https://v3b.fal.media/files/b/0aa12799/GovxON6TbyG74bdsNpoOd_speech.mp3
  2. Для каждого `provider_voice` эталон прогнан через FAL_VOICE_CHANGER_MODEL
     (fal-ai/elevenlabs/voice-changer, voice=<provider_voice>) реальным queue-вызовом
     (submit → poll result по request_id).
  3. URL результата — durable-хостинг fal (v3b.fal.media), используется напрямую.
     Каждый проверен: HTTP 200, content-type audio/mpeg. Длительность ~5с (probe/fal).

Значения — реальные рабочие fal-URL (не заглушки).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_seed_preset_voice_previews"
down_revision: str | None = "0013_video_stages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# key -> (preview_url, sample_duration_seconds). Реальные fal-результаты (2026-07-06).
PREVIEWS: list[tuple[str, str, int]] = [
    ("aria", "https://v3b.fal.media/files/b/0aa1278f/O1X8ithlPOuFfSfTWRJ_M_voice_changed.mp3", 5),
    ("max", "https://v3b.fal.media/files/b/0aa12790/PWl7NkkEfsqdCDRBnmS_Y_voice_changed.mp3", 5),
    ("luna", "https://v3b.fal.media/files/b/0aa12791/v9052039CwOCcw5kouAFv_voice_changed.mp3", 5),
    ("kai", "https://v3b.fal.media/files/b/0aa12791/PH8JESbUawQ9rMEpraHC6_voice_changed.mp3", 5),
    ("nova", "https://v3b.fal.media/files/b/0aa12792/A4ktbYq_cgFkwT5numsQg_voice_changed.mp3", 5),
    ("leo", "https://v3b.fal.media/files/b/0aa12793/UyS1jg1ZhTetOjZsAv0lP_voice_changed.mp3", 5),
    ("sage", "https://v3b.fal.media/files/b/0aa12794/UjNGoNeuBRUWr2qNd_biW_voice_changed.mp3", 5),
    ("rex", "https://v3b.fal.media/files/b/0aa12794/Vd8u7Y5LsNLQTAHsqpwIB_voice_changed.mp3", 5),
]


def upgrade() -> None:
    stmt = sa.text(
        "UPDATE preset_voices "
        "SET preview_url = :url, sample_duration_seconds = :dur "
        "WHERE key = :key"
    )
    bind = op.get_bind()
    for key, url, dur in PREVIEWS:
        bind.execute(stmt, {"key": key, "url": url, "dur": dur})


def downgrade() -> None:
    keys = [k for k, _url, _dur in PREVIEWS]
    stmt = sa.text(
        "UPDATE preset_voices "
        "SET preview_url = NULL, sample_duration_seconds = NULL "
        "WHERE key = ANY(:keys)"
    )
    op.get_bind().execute(stmt, {"keys": keys})
