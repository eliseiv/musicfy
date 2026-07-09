from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.schemas.common import CamelModel, StrippedNonEmpty


class RenameVoiceRequest(CamelModel):
    """Переименование профиля голоса (ADR-012). Пусто/пробелы → 400 INVALID_INPUT."""

    name: StrippedNonEmpty = Field(max_length=120, description="Новое имя профиля голоса.")


class ConsentRequest(CamelModel):
    """Согласие на использование голоса. `accepted` обязан быть true."""

    kind: str = Field(description="own_voice | third_party_authorized")
    accepted: bool = Field(description="Подтверждение согласия (должно быть true).")
    statement: str | None = Field(default=None, max_length=2000)


class ConsentResponse(CamelModel):
    id: str
    kind: str
    accepted: bool


class CreateVoiceRequest(CamelModel):
    """Клонирование голоса. Сначала `POST /v1/voices/consent`, затем загрузка образца."""

    sample_asset_url: str = Field(
        min_length=1, max_length=1024, description="URL образца голоса (из /v1/uploads/voice)."
    )
    consent_id: str = Field(description="ID согласия из /v1/voices/consent.")
    name: str | None = Field(default=None, max_length=120, description="Имя профиля голоса.")


class VoiceProfileResponse(CamelModel):
    id: str
    name: str | None
    status: str
    provider_voice_id: str | None = None
    preview_url: str | None = None
    sample_duration_seconds: int | None = None
    job_id: str | None = None
    created_at: datetime
