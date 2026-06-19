from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.schemas.common import CamelModel


class GenerateLyricsRequest(CamelModel):
    """Синхронная генерация текста песни. Результат можно править через PATCH."""

    prompt: str = Field(min_length=1, max_length=2000, description="Тема/идея песни.")
    language: str = Field(default="en", max_length=8, description="Язык (ISO-код).")
    genre: str | None = Field(default=None, max_length=64)
    mood: str | None = Field(default=None, max_length=64)


class UpdateLyricsRequest(CamelModel):
    content: str = Field(min_length=1, max_length=8000)


class LyricsDraftResponse(CamelModel):
    id: str
    content: str
    language: str
    genre: str | None
    mood: str | None
    source: str
    created_at: datetime
