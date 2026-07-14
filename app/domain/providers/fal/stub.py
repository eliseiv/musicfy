"""In-process stub for FalProvider — для dev/смоук-тестов без реального fal.ai.

Активируется флагом `FAL_USE_STUB=true`. Возвращает синтетические ответы и
подписывает webhook'и тем же `FAL_WEBHOOK_SECRET`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping

from app.domain.providers.fal.base import (
    FalStatusResult,
    FalSubmitResult,
    FalWebhookEvent,
)
from app.domain.providers.fal.parsing import parse_fal_webhook_event
from app.domain.providers.fal.signature import verify_signature

logger = logging.getLogger(__name__)


class StubFalProvider:
    PROVIDER_NAME = "fal-stub"

    def __init__(
        self,
        *,
        webhook_secret: str = "",
        video_lyrics_bg_model: str = "",
        voice_conversion_model: str = "",
    ) -> None:
        self._webhook_secret = webhook_secret
        # Симметрия конструктора с FalAiProvider; стаб отдаёт синтетику, поэтому
        # реальную модель не дёргает (submit-и игнорируют строки моделей).
        self._video_lyrics_bg_model = video_lyrics_bg_model
        self._voice_conversion_model = voice_conversion_model

    async def aclose(self) -> None:
        pass

    def _submit(self, kind: str, duration: float | None = None) -> FalSubmitResult:
        request_id = f"stub-{kind}-{uuid.uuid4().hex[:8]}"
        logger.info("StubFal: submit %s request_id=%s", kind, request_id)
        return FalSubmitResult(
            request_id=request_id,
            status="queued",
            duration_seconds=duration,
            raw={"stub": True, "model": kind},
        )

    async def submit_song(
        self,
        *,
        prompt: str,
        duration_seconds: float | None,
        lyrics: str | None,
        reference_audio_url: str | None,
        webhook_url: str | None,
        idempotency_key: str,
    ) -> FalSubmitResult:
        return self._submit("song", duration_seconds)

    async def submit_audio_to_audio_refine(
        self, *, source_audio_url, prompt, webhook_url, idempotency_key
    ) -> FalSubmitResult:
        return self._submit("refine")

    async def submit_stable_audio(
        self, *, prompt, seconds_total, webhook_url, idempotency_key
    ) -> FalSubmitResult:
        return self._submit("stable-audio")

    async def submit_ace_step_vocal(
        self, *, tags, lyrics, webhook_url, idempotency_key
    ) -> FalSubmitResult:
        return self._submit("ace-step")

    async def generate_lyrics(self, *, prompt, language="en", genre=None, mood=None) -> str:
        return (
            f"[Verse]\nStub lyrics for theme: {prompt[:40]}\nLine two of the verse\n\n"
            "[Chorus]\nThis is the stub chorus line\nSinging out into the night"
        )

    async def submit_speech(
        self, *, text, voice_id, webhook_url, idempotency_key
    ) -> FalSubmitResult:
        return self._submit("speech")

    async def voice_clone(self, *, audio_url: str) -> str:
        return f"stub-voice-{uuid.uuid4().hex[:12]}"

    async def submit_stem_separation(
        self, *, audio_url, webhook_url, idempotency_key
    ) -> FalSubmitResult:
        return self._submit("demucs")

    async def submit_voice_changer(
        self, *, audio_url, target_voice, webhook_url, idempotency_key
    ) -> FalSubmitResult:
        return self._submit("voice-changer")

    async def submit_speech_to_speech(
        self, *, source_audio_url, target_voice_audio_url, webhook_url, idempotency_key
    ) -> FalSubmitResult:
        # ADR-009: клон-ветка cover. Симметрично voice-changer стабу — синтетический
        # queued-результат; форма готового результата ({"audio": {"url"}}) эмитится
        # тестовым webhook'ом через общий parse_fal_webhook_event, как у voice-changer.
        return self._submit("speech-to-speech")

    async def submit_lipsync_video(
        self, *, video_url, audio_url, webhook_url, idempotency_key
    ) -> FalSubmitResult:
        return self._submit("lipsync")

    async def submit_avatar_image_video(
        self, *, image_url, audio_url, webhook_url, idempotency_key
    ) -> FalSubmitResult:
        return self._submit("avatar-image")

    async def submit_text_to_video(
        self,
        *,
        prompt,
        aspect_ratio,
        webhook_url,
        idempotency_key,
        resolution=None,
        generate_audio=False,
        duration=None,
    ) -> FalSubmitResult:
        return self._submit("t2v")

    async def submit_lyrics_background(
        self,
        *,
        prompt,
        aspect_ratio,
        webhook_url,
        idempotency_key,
        resolution=None,
        generate_audio=False,
        duration=None,
    ) -> FalSubmitResult:
        return self._submit("lyrics-bg")

    async def submit_image_to_video(
        self,
        *,
        prompt,
        image_url,
        aspect_ratio,
        webhook_url,
        idempotency_key,
        resolution=None,
        generate_audio=False,
        duration=None,
    ) -> FalSubmitResult:
        return self._submit("i2v")

    async def upload_to_storage(self, *, content: bytes, filename: str, content_type: str) -> str:
        return f"https://fal-stub-cdn.local/{uuid.uuid4().hex}/{filename}"

    async def fetch_status(
        self, *, model: str, request_id: str, status_url=None, response_url=None
    ) -> FalStatusResult:
        # Stub всегда IN_QUEUE — в тестах продвигаем пайплайн через emit_webhook.
        return FalStatusResult(request_id=request_id, status="IN_QUEUE", raw={"stub": True})

    async def verify_webhook(self, *, headers: Mapping[str, str], raw_body: bytes) -> None:
        verify_signature(secret=self._webhook_secret, raw_body=raw_body, headers=headers)

    def parse_webhook_event(
        self, *, headers: Mapping[str, str], raw_body: bytes
    ) -> FalWebhookEvent:
        # Идентично FalAiProvider: единый парсер контракта fal queue webhook,
        # чтобы стаб и реальный провайдер физически не могли разойтись.
        return parse_fal_webhook_event(raw_body)
