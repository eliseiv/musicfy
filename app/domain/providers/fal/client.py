from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from typing import Any

import httpx

from app.api.errors import (
    FalProviderError,
    FalTimeout,
    WebhookPayloadInvalid,
    WebhookSignatureInvalid,
)
from app.domain.providers.fal.base import (
    FalStatusResult,
    FalSubmitResult,
    FalWebhookEvent,
)
from app.domain.providers.fal.signature import (
    FAL_JWKS_URL,
    body_digest,
    has_fal_ed25519_headers,
    verify_fal_ed25519,
    verify_signature,
)
from app.logging_config import provider_var

logger = logging.getLogger(__name__)

# fal storage API живёт на rest.alpha.fal.ai (не queue.fal.run).
REST_STORAGE_BASE = "https://rest.alpha.fal.ai"
# Sync (не queue) вызовы — на fal.run.
SYNC_BASE = "https://fal.run"


def _extract_media(obj: Any) -> tuple[str | None, float | None]:
    """Достаёт media-url и длительность из разных форматов ответа fal."""
    media_url: str | None = None
    duration: float | None = None
    if not isinstance(obj, dict):
        return None, None
    for key in (
        "audio",
        "audio_file",
        "video",
        "video_file",
        "output",
        "result",
    ):
        v = obj.get(key)
        if isinstance(v, dict):
            media_url = media_url or v.get("url") or v.get("audio_url") or v.get("video_url")
            duration = duration or v.get("duration") or v.get("duration_seconds")
        elif isinstance(v, str):
            media_url = media_url or v
    media_url = media_url or obj.get("audio_url") or obj.get("video_url")
    duration = duration or obj.get("duration_seconds") or obj.get("duration")
    return media_url, (float(duration) if duration is not None else None)


class FalAiProvider:
    """Async client for fal.ai queue API + storage uploads (musicfy)."""

    PROVIDER_NAME = "fal"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        song_model: str,
        refine_model: str,
        speech_model: str,
        voice_clone_model: str,
        lyrics_llm: str,
        demucs_model: str,
        voice_changer_model: str,
        video_model: str,
        webhook_secret: str,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not api_key:
            raise RuntimeError("FAL_API_KEY is required")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._song_model = song_model
        self._refine_model = refine_model
        self._speech_model = speech_model
        self._voice_clone_model = voice_clone_model
        self._lyrics_llm = lyrics_llm
        self._demucs_model = demucs_model
        self._voice_changer_model = voice_changer_model
        self._video_model = video_model
        self._webhook_secret = webhook_secret
        self._timeout = timeout_seconds
        self._fal_jwks: list[dict] = []
        self._fal_jwks_at: float = 0.0
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            headers={"Authorization": f"Key {api_key}"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------- song ----------

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
        # Схема fal-ai/minimax-music/v2.6: prompt (10..2000) + опц. lyrics (<=3500,
        # с тегами [Verse]/[Chorus]). duration_seconds и reference_audio_url моделью
        # НЕ поддерживаются — не отправляем (иначе 422).
        safe_prompt = (prompt or "").strip()
        if len(safe_prompt) < 10:
            safe_prompt = (safe_prompt + " — original music track").strip()
        payload: dict[str, Any] = {"prompt": safe_prompt[:2000]}
        if lyrics:
            payload["lyrics"] = lyrics[:3500]
        else:
            payload["lyrics_optimizer"] = True
        return await self._submit(
            model=self._song_model,
            payload=payload,
            webhook_url=webhook_url,
            idempotency_key=idempotency_key,
        )

    async def submit_audio_to_audio_refine(
        self,
        *,
        source_audio_url: str,
        prompt: str,
        webhook_url: str | None,
        idempotency_key: str,
    ) -> FalSubmitResult:
        return await self._submit(
            model=self._refine_model,
            payload={"audio_url": source_audio_url, "prompt": prompt},
            webhook_url=webhook_url,
            idempotency_key=idempotency_key,
        )

    async def submit_stable_audio(
        self,
        *,
        prompt: str,
        seconds_total: int,
        webhook_url: str | None,
        idempotency_key: str,
    ) -> FalSubmitResult:
        payload: dict[str, Any] = {
            "prompt": prompt,
            "seconds_total": max(1, min(47, int(seconds_total))),
        }
        return await self._submit(
            model="fal-ai/stable-audio",
            payload=payload,
            webhook_url=webhook_url,
            idempotency_key=idempotency_key,
        )

    async def submit_ace_step_vocal(
        self,
        *,
        tags: str,
        lyrics: str,
        webhook_url: str | None,
        idempotency_key: str,
    ) -> FalSubmitResult:
        return await self._submit(
            model="fal-ai/ace-step",
            payload={"tags": tags, "lyrics": lyrics},
            webhook_url=webhook_url,
            idempotency_key=idempotency_key,
        )

    # ---------- lyrics ----------

    async def generate_lyrics(
        self,
        *,
        prompt: str,
        language: str = "en",
        genre: str | None = None,
        mood: str | None = None,
    ) -> str:
        """Sync-вызов fal-ai/any-llm для генерации структурированного текста песни."""
        lang_name = {
            "en": "English",
            "ru": "Russian",
            "es": "Spanish",
            "fr": "French",
            "de": "German",
            "pt": "Portuguese",
        }.get(language.lower()[:2], "English")
        hints = []
        if genre:
            hints.append(f"genre: {genre}")
        if mood:
            hints.append(f"mood: {mood}")
        hint_str = (" (" + ", ".join(hints) + ")") if hints else ""
        system = (
            f"You are a professional songwriter. Write song lyrics in {lang_name}{hint_str} "
            "based on the user's theme. Structure the song with clearly labelled sections "
            "using [Verse], [Chorus], [Bridge] markers. Output ONLY the lyrics — no preamble, "
            "no commentary, no markdown emphasis, no surrounding quotes."
        )
        full_prompt = f"{system}\n\nTheme: {prompt}\n\nLyrics:"

        url = f"{SYNC_BASE}/fal-ai/any-llm"
        body = {"model": self._lyrics_llm, "prompt": full_prompt}
        token = provider_var.set(self.PROVIDER_NAME)
        try:
            try:
                resp = await self._client.post(url, json=body, timeout=60.0)
            except httpx.TimeoutException as exc:
                raise FalTimeout() from exc
            except httpx.HTTPError as exc:
                raise FalProviderError(
                    f"fal LLM call failed: {exc.__class__.__name__}: {exc}"
                ) from exc
            if resp.status_code >= 400:
                raise FalProviderError(
                    f"fal LLM returned {resp.status_code}: {resp.text[:200]}"
                )
            data = resp.json()
            output = str(data.get("output") or "").strip()
            for marker in (
                "Lyrics:\n",
                "lyrics:\n",
                "Here are",
                "Here is",
                "Here's",
                "Sure,",
                "Sure!",
            ):
                if marker in output[:80]:
                    parts = output.split("\n\n", 1)
                    if len(parts) == 2:
                        output = parts[1].strip()
                    break
            output = output.replace("**", "").replace("__", "").replace("`", "")
            output = output.strip().strip("\"'").strip()
            return output
        finally:
            provider_var.reset(token)

    # ---------- vocal / voice ----------

    async def submit_speech(
        self,
        *,
        text: str,
        voice_id: str | None,
        webhook_url: str | None,
        idempotency_key: str,
    ) -> FalSubmitResult:
        payload: dict[str, Any] = {"text": text}
        if voice_id:
            payload["voice_setting"] = {"voice_id": voice_id}
        return await self._submit(
            model=self._speech_model,
            payload=payload,
            webhook_url=webhook_url,
            idempotency_key=idempotency_key,
        )

    async def voice_clone(self, *, audio_url: str) -> str:
        """Клонирует голос (minimax/voice-clone, sync), возвращает custom_voice_id."""
        url = f"{SYNC_BASE}/{self._voice_clone_model}"
        body = {"audio_url": audio_url}
        token = provider_var.set(self.PROVIDER_NAME)
        try:
            try:
                resp = await self._client.post(url, json=body, timeout=120.0)
            except httpx.TimeoutException as exc:
                raise FalTimeout() from exc
            except httpx.HTTPError as exc:
                raise FalProviderError(
                    f"voice_clone failed: {exc.__class__.__name__}: {exc}"
                ) from exc
            if resp.status_code >= 400:
                raise FalProviderError(
                    f"voice_clone returned {resp.status_code}: {resp.text[:200]}"
                )
            data = resp.json()
            voice_id = data.get("custom_voice_id") or data.get("voice_id")
            if not voice_id:
                raise FalProviderError(
                    f"voice_clone no custom_voice_id in response: {data}"
                )
            return str(voice_id)
        finally:
            provider_var.reset(token)

    # ---------- cover ----------

    async def submit_stem_separation(
        self,
        *,
        audio_url: str,
        webhook_url: str | None,
        idempotency_key: str,
    ) -> FalSubmitResult:
        return await self._submit(
            model=self._demucs_model,
            payload={"audio_url": audio_url},
            webhook_url=webhook_url,
            idempotency_key=idempotency_key,
        )

    async def submit_voice_changer(
        self,
        *,
        audio_url: str,
        target_voice: str,
        webhook_url: str | None,
        idempotency_key: str,
    ) -> FalSubmitResult:
        payload: dict[str, Any] = {"audio_url": audio_url, "voice": target_voice}
        return await self._submit(
            model=self._voice_changer_model,
            payload=payload,
            webhook_url=webhook_url,
            idempotency_key=idempotency_key,
        )

    # ---------- video ----------

    async def submit_lipsync_video(
        self,
        *,
        video_url: str,
        audio_url: str,
        webhook_url: str | None,
        idempotency_key: str,
    ) -> FalSubmitResult:
        payload: dict[str, Any] = {"video_url": video_url, "audio_url": audio_url}
        return await self._submit(
            model=self._video_model,
            payload=payload,
            webhook_url=webhook_url,
            idempotency_key=idempotency_key,
        )

    # ---------- storage ----------

    async def upload_to_storage(
        self,
        *,
        content: bytes,
        filename: str,
        content_type: str,
    ) -> str:
        token = provider_var.set(self.PROVIDER_NAME)
        try:
            initiate_url = f"{REST_STORAGE_BASE}/storage/upload/initiate"
            try:
                init_resp = await self._client.post(
                    initiate_url,
                    json={"file_name": filename, "content_type": content_type},
                )
            except httpx.TimeoutException as exc:
                raise FalTimeout() from exc
            except httpx.HTTPError as exc:
                raise FalProviderError(
                    f"fal storage initiate failed: {exc.__class__.__name__}"
                ) from exc
            if init_resp.status_code >= 400:
                raise FalProviderError(
                    f"fal storage initiate rejected ({init_resp.status_code}): "
                    f"{init_resp.text[:200]}"
                )
            init_data = init_resp.json()
            upload_url = init_data.get("upload_url")
            file_url = init_data.get("file_url")
            if not upload_url or not file_url:
                raise FalProviderError(
                    "fal storage initiate response missing upload_url/file_url"
                )

            try:
                put_resp = await self._client.put(
                    upload_url,
                    content=content,
                    headers={"Content-Type": content_type},
                )
            except httpx.TimeoutException as exc:
                raise FalTimeout() from exc
            except httpx.HTTPError as exc:
                raise FalProviderError(
                    f"fal storage PUT failed: {exc.__class__.__name__}"
                ) from exc
            if put_resp.status_code >= 400:
                raise FalProviderError(
                    f"fal storage PUT rejected ({put_resp.status_code}): "
                    f"{put_resp.text[:200]}"
                )
            return file_url
        finally:
            provider_var.reset(token)

    # ---------- webhooks ----------

    async def verify_webhook(
        self, *, headers: Mapping[str, str], raw_body: bytes
    ) -> None:
        # Production fal подписывает ED25519 (заголовки X-Fal-Webhook-*). Dev/локально
        # допускаем HMAC (X-Fal-Signature) — если ED25519-заголовков нет.
        if has_fal_ed25519_headers(headers):
            keys = await self._get_fal_jwks()
            verify_fal_ed25519(
                headers=headers, raw_body=raw_body, jwk_keys=keys, now=time.time()
            )
            return
        verify_signature(
            secret=self._webhook_secret, raw_body=raw_body, headers=headers
        )

    async def _get_fal_jwks(self) -> list[dict]:
        now = time.time()
        if self._fal_jwks and (now - self._fal_jwks_at) < 86400:
            return self._fal_jwks
        try:
            resp = await self._client.get(FAL_JWKS_URL)
            resp.raise_for_status()
            keys = resp.json().get("keys", [])
        except (httpx.HTTPError, ValueError) as exc:
            if self._fal_jwks:
                logger.warning("fal JWKS refresh failed, using cached: %s", exc)
                return self._fal_jwks
            raise WebhookSignatureInvalid(details={"reason": "jwks_unavailable"}) from exc
        if isinstance(keys, list) and keys:
            self._fal_jwks = keys
            self._fal_jwks_at = now
        return self._fal_jwks

    def parse_webhook_event(
        self, *, headers: Mapping[str, str], raw_body: bytes
    ) -> FalWebhookEvent:
        try:
            data = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WebhookPayloadInvalid(details={"reason": "not_json"}) from exc
        if not isinstance(data, dict):
            raise WebhookPayloadInvalid(details={"reason": "not_object"})

        request_id = data.get("request_id") or data.get("requestId") or data.get("id")
        if not request_id:
            raise WebhookPayloadInvalid(details={"reason": "no_request_id"})

        status = (data.get("status") or "").lower()
        if status not in {"completed", "failed", "canceled", "in_progress"}:
            if status in {"ok", "success"}:
                status = "completed"
            else:
                raise WebhookPayloadInvalid(
                    details={"reason": "unknown_status", "status": status}
                )

        result = data.get("result") or data.get("output") or {}
        if not isinstance(result, dict):
            result = {}
        media_url, duration_seconds = _extract_media(result)
        stems = result.get("stems") if isinstance(result.get("stems"), dict) else None
        error_message = data.get("error") or data.get("error_message")

        event_id = (
            data.get("event_id") or data.get("eventId") or f"{request_id}:{status}"
        )

        return FalWebhookEvent(
            request_id=str(request_id),
            status=status,
            media_url=media_url,
            duration_seconds=duration_seconds,
            stems=stems,
            error_message=str(error_message) if error_message else None,
            raw=data,
            event_id=str(event_id),
            payload_digest=body_digest(raw_body),
        )

    # ---------- private ----------

    async def _submit(
        self,
        *,
        model: str,
        payload: dict[str, Any],
        webhook_url: str | None,
        idempotency_key: str,
    ) -> FalSubmitResult:
        url = f"{self._base_url}/{model}"
        params = {}
        headers: dict[str, str] = {"X-Idempotency-Key": idempotency_key}
        if webhook_url:
            params["fal_webhook"] = webhook_url
        token = provider_var.set(self.PROVIDER_NAME)
        try:
            try:
                response = await self._client.post(
                    url, json=payload, params=params, headers=headers
                )
            except httpx.TimeoutException as exc:
                raise FalTimeout() from exc
            except httpx.HTTPError as exc:
                raise FalProviderError(
                    f"fal submit failed: {exc.__class__.__name__}: {exc}"
                ) from exc
            if response.status_code >= 500:
                raise FalProviderError(f"fal returned {response.status_code} for {model}")
            if response.status_code >= 400:
                raise FalProviderError(
                    f"fal rejected submit ({response.status_code}): {response.text[:200]}"
                )
            try:
                data = response.json()
            except ValueError as exc:
                raise FalProviderError("fal returned non-JSON body") from exc
            request_id = (
                data.get("request_id") or data.get("requestId") or data.get("id")
            )
            if not request_id:
                raise FalProviderError("fal response missing request_id")
            media_url, duration = _extract_media(data)
            return FalSubmitResult(
                request_id=str(request_id),
                status=(data.get("status") or "queued").lower(),
                media_url=media_url,
                duration_seconds=duration,
                status_url=data.get("status_url"),
                response_url=data.get("response_url"),
                raw=data,
            )
        finally:
            provider_var.reset(token)

    async def fetch_status(
        self,
        *,
        model: str,
        request_id: str,
        status_url: str | None = None,
        response_url: str | None = None,
    ) -> FalStatusResult:
        token = provider_var.set(self.PROVIDER_NAME)
        try:
            # Приоритет — реальные URL из ответа submit. Конструируемый из versioned
            # пути URL ненадёжен (fal-ai/.../v2.6 → 404).
            status_u = status_url or f"{self._base_url}/{model}/requests/{request_id}/status"
            result_u = response_url or f"{self._base_url}/{model}/requests/{request_id}"
            try:
                resp = await self._client.get(status_u)
            except httpx.TimeoutException as exc:
                raise FalTimeout() from exc
            except httpx.HTTPError as exc:
                raise FalProviderError(
                    f"fal status fetch failed: {exc.__class__.__name__}"
                ) from exc
            if resp.status_code == 404:
                return FalStatusResult(
                    request_id=request_id, status="IN_QUEUE", raw={}
                )
            if resp.status_code >= 400:
                raise FalProviderError(
                    f"fal status returned {resp.status_code}: {resp.text[:200]}"
                )
            data = resp.json()
            status = str(data.get("status") or "").upper()

            if status != "COMPLETED":
                return FalStatusResult(
                    request_id=request_id, status=status or "IN_QUEUE", raw=data
                )

            try:
                result_resp = await self._client.get(result_u)
            except (httpx.TimeoutException, httpx.HTTPError) as exc:
                raise FalProviderError(
                    f"fal result fetch failed: {exc.__class__.__name__}"
                ) from exc
            if result_resp.status_code == 422:
                detail = result_resp.text[:300]
                return FalStatusResult(
                    request_id=request_id,
                    status="FAILED",
                    error_message=f"422 Unprocessable: {detail}",
                    raw={"status_code": 422, "body": detail},
                )
            if result_resp.status_code >= 400:
                return FalStatusResult(
                    request_id=request_id,
                    status="FAILED",
                    error_message=(
                        f"result {result_resp.status_code}: {result_resp.text[:200]}"
                    ),
                    raw={},
                )
            result_data = result_resp.json()
            media_url, duration = _extract_media(result_data)
            stems_field = result_data.get("stems")
            stems = stems_field if isinstance(stems_field, dict) else None

            return FalStatusResult(
                request_id=request_id,
                status="COMPLETED",
                media_url=media_url,
                duration_seconds=duration,
                stems=stems,
                raw=result_data,
            )
        finally:
            provider_var.reset(token)
