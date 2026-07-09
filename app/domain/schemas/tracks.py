from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.schemas.common import CamelModel, StrippedNonEmpty


class RenameTrackRequest(CamelModel):
    """Переименование трека (ADR-012). Пусто/пробелы → 400 INVALID_INPUT."""

    title: StrippedNonEmpty = Field(max_length=255, description="Новое название трека.")


class TrackVariantView(CamelModel):
    id: str
    variant_index: int
    audio_url: str
    duration_seconds: float
    stems: dict | None = None


class TrackResponse(CamelModel):
    id: str
    kind: str
    title: str | None
    prompt: str | None = None
    job_id: str | None
    created_at: datetime
    variants: list[TrackVariantView]


class TrackSummary(CamelModel):
    id: str
    kind: str
    title: str | None
    created_at: datetime


class PresetView(CamelModel):
    key: str
    title: str
    subtitle: str | None = None
    prompt_text: str | None = None
