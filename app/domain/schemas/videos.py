from __future__ import annotations

from datetime import datetime

from pydantic import ConfigDict, Field

from app.schemas.common import CamelModel


class CreateVideoRequest(CamelModel):
    """AI music video. `audioUrl` — готовый трек, `sourceVideoUrl` — аватар/исходное видео."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "audioUrl": "https://fal.media/files/.../song.mp3",
                "sourceVideoUrl": "https://fal.media/files/.../avatar.mp4",
                "mode": "avatar_performance",
            }
        }
    )

    audio_url: str = Field(min_length=1, max_length=1024, description="URL аудио (трека).")
    source_video_url: str = Field(
        min_length=1, max_length=1024, description="URL исходного видео (из /v1/uploads/source-video)."
    )
    mode: str = Field(
        default="avatar_performance", max_length=32,
        description="Режим. В V1 — avatar_performance.",
    )
    title: str | None = Field(default=None, max_length=255)


class VideoResultResponse(CamelModel):
    job_id: str
    status: str = Field(description="Статус задачи (см. GET /v1/jobs/{jobId}).")
    video_url: str | None = Field(default=None, description="URL готового видео (если completed).")
    created_at: datetime | None = None


class PushTokenRequest(CamelModel):
    """Регистрация APNs-токена для push о готовности задач."""

    token: str = Field(min_length=1, max_length=255, description="APNs device token.")
    platform: str = Field(default="ios", max_length=16)
