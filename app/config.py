from __future__ import annotations

import uuid
from functools import lru_cache
from typing import Literal
from uuid import UUID

from pydantic import SecretStr, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_API_KEY_NAMESPACE = uuid.UUID("9c1d6f1a-2e44-4d0b-8a3c-7e1d2f4b6a90")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    APP_ENV: Literal["dev", "prod", "test"] = "dev"
    LOG_LEVEL: str = "INFO"
    HTTP_HOST: str = "0.0.0.0"
    HTTP_PORT: int = 8000

    DATABASE_URL: str
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_ECHO: bool = False

    # Сервисный ключ (internal/webhook), не пользовательский auth.
    API_KEY: str | None = None
    # Отдельный ключ для админ-эндпоинтов (начисление кредитов/подписки).
    ADMIN_API_KEY: str | None = None

    RATE_LIMIT_PER_MINUTE: int = 0
    RATE_LIMIT_BURST: int = 60

    PUBLIC_BASE_URL: str = ""

    # --- Сессии пользователей ---
    SESSION_TTL_SECONDS: int = 2_592_000  # 30 дней

    # --- Sign in with Apple ---
    APPLE_BUNDLE_ID: str = ""
    APPLE_ALLOWED_AUDIENCES: str = ""

    # --- fal.ai ---
    FAL_USE_STUB: bool = False
    FAL_API_KEY: SecretStr = SecretStr("")
    FAL_BASE_URL: str = "https://queue.fal.run"
    FAL_HTTP_TIMEOUT_SECONDS: float = 30.0
    FAL_WEBHOOK_SECRET: SecretStr = SecretStr("")
    FAL_SONG_MODEL: str = "fal-ai/minimax-music/v2.6"
    FAL_REFINE_MODEL: str = "fal-ai/ace-step/audio-to-audio"
    FAL_SPEECH_MODEL: str = "fal-ai/minimax/speech-02-turbo"
    FAL_VOICE_CLONE_MODEL: str = "fal-ai/minimax/voice-clone"
    FAL_LYRICS_LLM: str = "anthropic/claude-3-5-haiku"
    FAL_DEMUCS_MODEL: str = "fal-ai/demucs"
    FAL_VOICE_CHANGER_MODEL: str = "fal-ai/elevenlabs/voice-changer"
    FAL_VIDEO_MODEL: str = "fal-ai/kling-video/lipsync/audio-to-video"

    # --- App Store (StoreKit 2) ---
    APPLE_STOREKIT_ISSUER_ID: str = ""
    APPLE_STOREKIT_KEY_ID: str = ""
    APPLE_STOREKIT_PRIVATE_KEY: SecretStr = SecretStr("")
    APPLE_STOREKIT_BUNDLE_ID: str = ""
    APPLE_STOREKIT_ENVIRONMENT: Literal["Sandbox", "Production"] = "Sandbox"
    # Проверять подпись JWS-транзакций (x5c → Apple Root CA - G3). В production — true.
    # false только для dev/тестов с синтетическими токенами.
    APPLE_STOREKIT_VERIFY_SIGNATURE: bool = True

    # --- APNs ---
    APNS_ENABLED: bool = False
    APNS_KEY_ID: str = ""
    APNS_TEAM_ID: str = ""
    APNS_PRIVATE_KEY: SecretStr = SecretStr("")
    APNS_TOPIC: str = ""
    APNS_USE_SANDBOX: bool = True

    # --- Лимиты / загрузки ---
    MAX_CONCURRENT_GENERATIONS: int = 8
    UPLOAD_MAX_BYTES: int = 52_428_800  # 50 MiB
    UPLOAD_AUDIO_CONTENT_TYPES: str = (
        "audio/mpeg,audio/wav,audio/mp4,audio/x-m4a,audio/aac"
    )
    UPLOAD_VIDEO_CONTENT_TYPES: str = "video/mp4,video/quicktime"
    DEFAULT_TRACK_DURATION_SECONDS: int = 60
    JOB_HARD_TIMEOUT_SECONDS: int = 1800
    VIDEO_JOB_HARD_TIMEOUT_SECONDS: int = 5400

    URL_CHECK_ENABLED: bool = True
    URL_CHECK_TIMEOUT_SECONDS: float = 3.0

    POLL_ENABLED: bool = True
    POLL_INTERVAL_SECONDS: int = 10
    VIDEO_POLL_INTERVAL_SECONDS: int = 30

    @field_validator("API_KEY", "ADMIN_API_KEY", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v: object) -> object:
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    @computed_field  # type: ignore[prop-decorator]
    @property
    def api_user_id(self) -> UUID | None:
        if not self.API_KEY:
            return None
        return uuid.uuid5(_API_KEY_NAMESPACE, self.API_KEY)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def api_key_map(self) -> dict[str, UUID]:
        if not self.API_KEY:
            return {}
        user_id = self.api_user_id
        assert user_id is not None
        return {self.API_KEY: user_id}

    @computed_field  # type: ignore[prop-decorator]
    @property
    def apple_allowed_audiences(self) -> list[str]:
        raw = self.APPLE_ALLOWED_AUDIENCES or self.APPLE_BUNDLE_ID
        return [a.strip() for a in raw.split(",") if a.strip()]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def upload_audio_content_types(self) -> set[str]:
        return {c.strip() for c in self.UPLOAD_AUDIO_CONTENT_TYPES.split(",") if c.strip()}

    @computed_field  # type: ignore[prop-decorator]
    @property
    def upload_video_content_types(self) -> set[str]:
        return {c.strip() for c in self.UPLOAD_VIDEO_CONTENT_TYPES.split(",") if c.strip()}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
