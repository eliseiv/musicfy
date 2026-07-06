from __future__ import annotations

from pydantic import Field

from app.schemas.common import CamelModel


class PresetVoiceView(CamelModel):
    """Публичное представление пресет-голоса (AI Voices).

    Провайдерский `provider_voice` намеренно исключён — наружу отдаётся только
    стабильный `key`.
    """

    key: str = Field(description="Стабильный ключ пресета (передаётся в cover.targetVoice).")
    title: str
    subtitle: str | None = None
    preview_url: str | None = Field(default=None, description="URL превью-сэмпла (▶️).")
    sample_duration_seconds: int | None = Field(
        default=None, description="Длительность превью в секундах."
    )
    gender: str | None = None
    style: str | None = None
    language: str | None = None
