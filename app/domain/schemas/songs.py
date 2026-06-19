from __future__ import annotations

from pydantic import ConfigDict, Field

from app.schemas.common import CamelModel


class CreateSongRequest(CamelModel):
    """Параметры генерации песни. Минимум — `prompt` ИЛИ `lyricsPrompt`."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "prompt": "upbeat indie pop, summer road trip, bright synths",
                "genre": "pop",
                "mood": "happy",
                "language": "en",
                "lyricsPrompt": "a hopeful song about a summer road trip",
            }
        }
    )

    prompt: str | None = Field(default=None, max_length=2000, description="Описание стиля/идеи трека.")
    genre: str | None = Field(default=None, max_length=64, description="Жанр (см. /v1/presets/genres).")
    mood: str | None = Field(default=None, max_length=64, description="Настроение (см. /v1/presets/moods).")
    language: str = Field(default="en", max_length=8, description="Язык вокала (ISO-код).")
    tempo_bpm: int | None = Field(default=None, ge=40, le=240, description="Темп, BPM.")
    vocal_type: str | None = Field(default=None, max_length=64, description="Тип вокала (male/female/...).")
    custom_lyrics: str | None = Field(
        default=None, max_length=8000, description="Готовый текст песни (вместо генерации)."
    )
    lyrics_prompt: str | None = Field(
        default=None, max_length=2000, description="Тема для авто-генерации lyrics через LLM."
    )
    negative_hints: str | None = Field(default=None, max_length=500, description="Чего избегать.")
    voice_url: str | None = Field(
        default=None, max_length=1024, description="URL образца голоса для вокала (опционально)."
    )
    title: str | None = Field(default=None, max_length=255, description="Название трека.")
    desired_duration_seconds: int | None = Field(
        default=None, ge=10, le=300, description="Желаемая длительность, сек."
    )
    store_stems: bool = Field(default=False, description="Сохранять отдельные дорожки (stems).")


class JobAcceptedResponse(CamelModel):
    """Ответ на создание асинхронной задачи. Опрашивайте `GET /v1/jobs/{jobId}`."""

    job_id: str = Field(description="ID задачи для опроса статуса.")
    status: str = Field(description="Начальный статус (queued).")
    deduplicated: bool = Field(default=False, description="true — задача уже существовала (по Idempotency-Key).")
