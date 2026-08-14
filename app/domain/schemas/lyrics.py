from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from app.schemas.common import CamelModel


class GenerateLyricsRequest(CamelModel):
    """Синхронная генерация текста песни. Результат можно править через PATCH."""

    prompt: str = Field(min_length=1, max_length=2000, description="Тема/идея песни.")
    language: str = Field(default="en", max_length=8, description="Язык (ISO-код).")
    genre: str | None = Field(default=None, max_length=64)
    mood: str | None = Field(default=None, max_length=64)


class UpdateLyricsRequest(CamelModel):
    content: str = Field(min_length=1, max_length=8000)


class AssistTextRequest(CamelModel):
    """LLM-помощник для кнопок Inspire / Write for me / Enhance."""

    action: Literal["inspire", "write", "enhance"]
    target: Literal["lyrics", "prompt"]
    text: str | None = Field(
        default=None,
        max_length=8000,
        description=(
            "Исходный текст. Обязателен для enhance; для inspire/write может содержать тему."
        ),
    )
    language: str = Field(default="en", max_length=8)
    genre: str | None = Field(default=None, max_length=64)
    mood: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_text_for_action(self) -> AssistTextRequest:
        cleaned = (self.text or "").strip()
        if self.action == "enhance" and not cleaned:
            raise ValueError("text is required for enhance")
        self.text = cleaned or None
        return self


class AssistTextResponse(CamelModel):
    content: str
    suggested_title: str | None = None


class LyricsDraftResponse(CamelModel):
    id: str
    content: str
    language: str
    genre: str | None
    mood: str | None
    source: str
    created_at: datetime
