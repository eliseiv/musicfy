from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class FalSubmitResult:
    request_id: str
    status: str  # 'queued' | 'in_progress' | 'completed'
    media_url: str | None = None
    duration_seconds: float | None = None
    # status_url / response_url из ответа fal — использовать для опроса напрямую
    # (конструировать из versioned-пути модели нельзя: /v2.6 ломает URL → 404).
    status_url: str | None = None
    response_url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class FalStatusResult:
    """Результат опроса fal queue API (GET /requests/{rid}/status и /requests/{rid}).

    `status` — `IN_QUEUE` | `IN_PROGRESS` | `COMPLETED` | `FAILED` | `CANCELED`.
    `media_url` — основной результат (audio или video в зависимости от модели).
    """

    request_id: str
    status: str
    media_url: str | None = None
    duration_seconds: float | None = None
    stems: dict[str, Any] | None = None
    error_message: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class FalWebhookEvent:
    """Нормализованное событие webhook от fal."""

    request_id: str
    status: str  # 'completed' | 'failed' | 'canceled' | 'in_progress'
    media_url: str | None
    duration_seconds: float | None
    stems: dict[str, Any] | None
    error_message: str | None
    raw: dict[str, Any]
    event_id: str
    payload_digest: str


class FalProvider(Protocol):
    PROVIDER_NAME: str

    # ----- song -----
    async def submit_song(
        self,
        *,
        prompt: str,
        duration_seconds: float | None,
        lyrics: str | None,
        reference_audio_url: str | None,
        webhook_url: str | None,
        idempotency_key: str,
    ) -> FalSubmitResult: ...

    async def submit_audio_to_audio_refine(
        self,
        *,
        source_audio_url: str,
        prompt: str,
        webhook_url: str | None,
        idempotency_key: str,
    ) -> FalSubmitResult: ...

    async def submit_stable_audio(
        self,
        *,
        prompt: str,
        seconds_total: int,
        webhook_url: str | None,
        idempotency_key: str,
    ) -> FalSubmitResult: ...

    async def submit_ace_step_vocal(
        self,
        *,
        tags: str,
        lyrics: str,
        webhook_url: str | None,
        idempotency_key: str,
    ) -> FalSubmitResult: ...

    # ----- lyrics -----
    async def generate_lyrics(
        self,
        *,
        prompt: str,
        language: str = "en",
        genre: str | None = None,
        mood: str | None = None,
    ) -> str: ...

    # ----- vocal / voice -----
    async def submit_speech(
        self,
        *,
        text: str,
        voice_id: str | None,
        webhook_url: str | None,
        idempotency_key: str,
    ) -> FalSubmitResult: ...

    async def voice_clone(self, *, audio_url: str) -> str: ...

    # ----- cover -----
    async def submit_stem_separation(
        self,
        *,
        audio_url: str,
        webhook_url: str | None,
        idempotency_key: str,
    ) -> FalSubmitResult: ...

    async def submit_voice_changer(
        self,
        *,
        audio_url: str,
        target_voice: str,
        webhook_url: str | None,
        idempotency_key: str,
    ) -> FalSubmitResult: ...

    async def submit_speech_to_speech(
        self,
        *,
        source_audio_url: str,
        target_voice_audio_url: str,
        webhook_url: str | None,
        idempotency_key: str,
    ) -> FalSubmitResult:
        """Конвертация вокала в клон-голос (ADR-009, chatterbox speech-to-speech).

        target_voice_audio_url — аудио-образец целевого голоса (референс, zero-shot).
        """
        ...

    # ----- video -----
    async def submit_lipsync_video(
        self,
        *,
        video_url: str,
        audio_url: str,
        webhook_url: str | None,
        idempotency_key: str,
    ) -> FalSubmitResult: ...

    async def submit_avatar_image_video(
        self,
        *,
        image_url: str,
        audio_url: str,
        webhook_url: str | None,
        idempotency_key: str,
    ) -> FalSubmitResult: ...

    async def submit_text_to_video(
        self,
        *,
        prompt: str,
        aspect_ratio: str | None,
        webhook_url: str | None,
        idempotency_key: str,
    ) -> FalSubmitResult: ...

    async def submit_lyrics_background(
        self,
        *,
        prompt: str,
        aspect_ratio: str | None,
        webhook_url: str | None,
        idempotency_key: str,
    ) -> FalSubmitResult: ...

    async def submit_image_to_video(
        self,
        *,
        prompt: str,
        image_url: str,
        aspect_ratio: str | None,
        webhook_url: str | None,
        idempotency_key: str,
    ) -> FalSubmitResult: ...

    # ----- storage / webhooks / polling -----
    async def upload_to_storage(
        self,
        *,
        content: bytes,
        filename: str,
        content_type: str,
    ) -> str: ...

    async def verify_webhook(
        self, *, headers: Mapping[str, str], raw_body: bytes
    ) -> None:
        """Проверяет подпись webhook'а (ED25519 для prod fal, HMAC для dev/stub).

        Бросает WebhookSignatureInvalid при провале. Вызывается ДО parse.
        """
        ...

    def parse_webhook_event(
        self, *, headers: Mapping[str, str], raw_body: bytes
    ) -> FalWebhookEvent: ...

    async def fetch_status(
        self,
        *,
        model: str,
        request_id: str,
        status_url: str | None = None,
        response_url: str | None = None,
    ) -> FalStatusResult: ...
