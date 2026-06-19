"""In-process stub for FalProvider — для dev/смоук-тестов без реального fal.ai.

Активируется флагом `FAL_USE_STUB=true`. Возвращает синтетические ответы и
подписывает webhook'и тем же `FAL_WEBHOOK_SECRET`.
"""
from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping

from app.api.errors import WebhookPayloadInvalid
from app.domain.providers.fal.base import (
    FalStatusResult,
    FalSubmitResult,
    FalWebhookEvent,
)
from app.domain.providers.fal.signature import body_digest, verify_signature

logger = logging.getLogger(__name__)


class StubFalProvider:
    PROVIDER_NAME = "fal-stub"

    def __init__(self, *, webhook_secret: str = "") -> None:
        self._webhook_secret = webhook_secret

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

    async def generate_lyrics(
        self, *, prompt, language="en", genre=None, mood=None
    ) -> str:
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

    async def submit_lipsync_video(
        self, *, video_url, audio_url, webhook_url, idempotency_key
    ) -> FalSubmitResult:
        return self._submit("lipsync")

    async def upload_to_storage(
        self, *, content: bytes, filename: str, content_type: str
    ) -> str:
        return f"https://fal-stub-cdn.local/{uuid.uuid4().hex}/{filename}"

    async def fetch_status(
        self, *, model: str, request_id: str, status_url=None, response_url=None
    ) -> FalStatusResult:
        # Stub всегда IN_QUEUE — в тестах продвигаем пайплайн через emit_webhook.
        return FalStatusResult(request_id=request_id, status="IN_QUEUE", raw={"stub": True})

    async def verify_webhook(
        self, *, headers: Mapping[str, str], raw_body: bytes
    ) -> None:
        verify_signature(
            secret=self._webhook_secret, raw_body=raw_body, headers=headers
        )

    def parse_webhook_event(
        self, *, headers: Mapping[str, str], raw_body: bytes
    ) -> FalWebhookEvent:
        try:
            data = json.loads(raw_body.decode("utf-8"))
        except Exception as exc:
            raise WebhookPayloadInvalid(details={"reason": "not_json"}) from exc
        request_id = data.get("request_id") or data.get("id")
        if not request_id:
            raise WebhookPayloadInvalid(details={"reason": "no_request_id"})
        status_value = (data.get("status") or "completed").lower()
        result = data.get("result") or {}
        media_url = (
            result.get("media_url")
            or result.get("audio_url")
            or result.get("video_url")
        )
        return FalWebhookEvent(
            request_id=str(request_id),
            status=status_value,
            media_url=media_url,
            duration_seconds=result.get("duration_seconds"),
            stems=result.get("stems") if isinstance(result.get("stems"), dict) else None,
            error_message=data.get("error"),
            raw=data,
            event_id=str(data.get("event_id") or f"{request_id}:{status_value}"),
            payload_digest=body_digest(raw_body),
        )
