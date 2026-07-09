from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from app.domain.enums import VideoAspect, VideoMode, VideoStyle
from app.schemas.common import CamelModel, StrippedNonEmpty


class RenameVideoRequest(CamelModel):
    """Переименование видео (ADR-012). Пусто/пробелы → 400 INVALID_INPUT."""

    title: StrippedNonEmpty = Field(max_length=255, description="Новое название видео.")


class CreateVideoRequest(CamelModel):
    """AI music video на 3 режима (ADR-007).

    Источник аудио — ровно один из `audioUrl` / `trackId`. Остальные обязательные
    поля зависят от `mode`:
    - `avatar_performance` — аватар: `sourceVideoUrl` **или** `referenceImageUrl`;
    - `visual_clip` — `prompt` **или** `surpriseMe`;
    - `lyrics_video` — лирика: поле `lyrics` **или** `trackId` (лирика из трека).
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "mode": "avatar_performance",
                "audioUrl": "https://fal.media/files/.../song.mp3",
                "sourceVideoUrl": "https://fal.media/files/.../avatar.mp4",
                "aspectRatio": "9:16",
                "style": "cinematic",
            }
        }
    )

    mode: VideoMode = Field(description="Режим генерации видео.")
    audio_url: str | None = Field(
        default=None, min_length=1, max_length=1024, description="Прямой URL аудио (трека)."
    )
    track_id: UUID | None = Field(
        default=None, description="«My track» — источник аудио (и лирики для lyrics_video)."
    )
    variant_id: UUID | None = Field(
        default=None, description="Конкретный вариант трека (иначе — первый)."
    )
    source_video_url: str | None = Field(
        default=None, min_length=1, max_length=1024,
        description="Исходное видео (avatar_performance) из /v1/uploads/source-video.",
    )
    reference_image_url: str | None = Field(
        default=None, min_length=1, max_length=1024,
        description="Референс-картинка из /v1/uploads/image.",
    )
    style: VideoStyle | None = Field(default=None, description="Стиль: realistic/cartoon/anime/cinematic.")
    aspect_ratio: VideoAspect | None = Field(
        default=VideoAspect.vertical_9_16, description="Соотношение сторон (default 9:16)."
    )
    prompt: str | None = Field(default=None, max_length=2000, description="Сюжет/описание видеоряда.")
    lyrics: str | None = Field(
        default=None, max_length=5000,
        description="Явная лирика для lyrics_video (когда используется audioUrl без trackId).",
    )
    surprise_me: bool = Field(default=False, description="Серверный подбор промпта из пресетов.")
    title: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def _validate_by_mode(self) -> CreateVideoRequest:
        # Источник аудио — ровно один из audio_url / track_id.
        has_audio = bool(self.audio_url)
        has_track = self.track_id is not None
        if not has_audio and not has_track:
            raise ValueError("audio source required: provide audioUrl or trackId")
        if has_audio and has_track:
            raise ValueError("ambiguous audio source: provide either audioUrl or trackId")

        # source_video_url допустим только для avatar_performance.
        if self.source_video_url and self.mode != VideoMode.avatar_performance:
            raise ValueError("sourceVideoUrl is only valid for avatar_performance mode")

        if self.mode == VideoMode.avatar_performance:
            if not self.source_video_url and not self.reference_image_url:
                raise ValueError(
                    "avatar_performance requires sourceVideoUrl or referenceImageUrl"
                )
        elif self.mode == VideoMode.visual_clip:
            if not (self.prompt and self.prompt.strip()) and not self.surprise_me:
                raise ValueError("visual_clip requires prompt or surpriseMe")
        elif self.mode == VideoMode.lyrics_video:
            if not (self.lyrics and self.lyrics.strip()) and not has_track:
                raise ValueError("lyrics_video requires lyrics or trackId")
        return self


class VideoResultResponse(CamelModel):
    job_id: str
    status: str = Field(description="Статус задачи (см. GET /v1/jobs/{jobId}).")
    video_url: str | None = Field(default=None, description="URL готового видео (если completed).")
    mode: str | None = Field(default=None, description="Режим генерации (из Asset.meta).")
    aspect_ratio: str | None = Field(default=None, description="Соотношение сторон (из Asset.meta).")
    style: str | None = Field(default=None, description="Стиль (из Asset.meta).")
    title: str | None = Field(default=None, description="Название видео (из Asset.meta).")
    created_at: datetime | None = None


class PushTokenRequest(CamelModel):
    """Регистрация APNs-токена для push о готовности задач."""

    token: str = Field(min_length=1, max_length=255, description="APNs device token.")
    platform: str = Field(default="ios", max_length=16)
