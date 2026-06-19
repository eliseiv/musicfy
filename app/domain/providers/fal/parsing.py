"""Общий разбор fal webhook-конверта — единый источник истины для всех провайдеров.

Реальный fal queue webhook приходит в конверте:
``{"request_id": "<id>", "status": "OK"|"ERROR"|..., "payload": {<результат модели>},
"error": null}``.

Чтобы реальный (`FalAiProvider`) и стаб (`StubFalProvider`) провайдеры не могли
разойтись в трактовке контракта, оба обязаны вызывать функции этого модуля.
"""

from __future__ import annotations

import json
from typing import Any

from app.api.errors import WebhookPayloadInvalid
from app.domain.providers.fal.base import FalWebhookEvent
from app.domain.providers.fal.signature import body_digest

# Нормализованные статусы webhook-события.
_WEBHOOK_STATUSES = {"completed", "failed", "canceled", "in_progress"}
# Сырые статусы fal queue, означающие успешное завершение.
_OK_STATUSES = {"ok", "success"}


def extract_media(obj: Any) -> tuple[str | None, float | None]:
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


def parse_fal_webhook_event(raw_body: bytes) -> FalWebhookEvent:
    """Разбирает сырое тело fal webhook в нормализованное ``FalWebhookEvent``.

    Единый парсер контракта fal queue webhook: оба провайдера (реальный и стаб)
    вызывают именно его, чтобы их поведение было гарантированно идентичным.
    Бросает ``WebhookPayloadInvalid`` при невалидном теле/статусе.
    """
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
    if status not in _WEBHOOK_STATUSES:
        if status in _OK_STATUSES:
            status = "completed"
        else:
            raise WebhookPayloadInvalid(details={"reason": "unknown_status", "status": status})

    # fal queue доставляет webhook в конверте: верхнеуровневые request_id /
    # gateway_request_id / status / payload / error, где payload — сам результат
    # модели ({"audio": {...}}). payload — первый источник; result/output
    # сохранены для обратной совместимости с прямыми форматами / иными моделями.
    result = data.get("payload") or data.get("result") or data.get("output") or {}
    if not isinstance(result, dict):
        result = {}
    media_url, duration_seconds = extract_media(result)
    stems = result.get("stems") if isinstance(result.get("stems"), dict) else None
    error_message = data.get("error") or data.get("error_message")

    event_id = data.get("event_id") or data.get("eventId") or f"{request_id}:{status}"

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
