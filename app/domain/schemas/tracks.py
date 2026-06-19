from __future__ import annotations

from datetime import datetime

from app.schemas.common import CamelModel


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
