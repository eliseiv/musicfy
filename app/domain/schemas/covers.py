from __future__ import annotations

from pydantic import ConfigDict, Field

from app.schemas.common import CamelModel


class CreateCoverRequest(CamelModel):
    """AI-кавер: загрузите аудио через `POST /v1/uploads/audio`, передайте его `url`."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "sourceAudioUrl": "https://fal.media/files/.../input.mp3",
                "targetVoice": "english_male",
            }
        }
    )

    source_audio_url: str = Field(
        min_length=1, max_length=1024, description="URL исходного аудио (из /v1/uploads/audio)."
    )
    target_voice: str | None = Field(
        default=None, max_length=128, description="Целевой голос (пресет или voiceId профиля)."
    )
    title: str | None = Field(default=None, max_length=255, description="Название кавера.")
    store_stems: bool = Field(default=False, description="Сохранять отдельные дорожки.")


class AssetResponse(CamelModel):
    """Загруженный медиа-ассет."""

    asset_id: str = Field(description="ID ассета.")
    url: str = Field(description="URL для использования в запросах генерации.")
    kind: str = Field(description="Тип: audio / video / voice_sample / source_video.")
    mime: str | None = None
