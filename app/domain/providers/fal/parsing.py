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
# Маппинг сырых статусов fal queue в нормализованное множество.
# Новых нормализованных статусов не вводится — алиасы fal лишь сводятся к существующим.
_STATUS_ALIASES = {
    "ok": "completed",
    "success": "completed",
    "error": "failed",
    "failed": "failed",
    "canceled": "canceled",
    "cancelled": "canceled",
    "in_progress": "in_progress",
}
# Максимальная длина error_message после компактной сериализации fallback-источников.
_ERROR_MESSAGE_MAX_LEN = 500

# Известный словарь имён стемов (demucs + другие сепараторы).
STEM_NAMES = {
    "vocals",
    "vocal",
    "drums",
    "bass",
    "other",
    "guitar",
    "piano",
    "accompaniment",
    "instrumental",
    "backing",
}


def extract_stems(result: dict) -> dict | None:
    """Извлекает стемы из результата fal-сепаратора.

    Приоритет — явная обёртка ``result["stems"]`` (wrapped-модели, обратная
    совместимость). Иначе demucs-путь: собрать верхнеуровневые ключи из
    ``STEM_NAMES``, у которых значение даёт url (``{"url": ..}`` или строка).
    Порог ``>=2`` защищает от коллизии одиночного stem-именованного ключа
    у не-сепараторных моделей; demucs всегда отдаёт 4-6 стемов.
    """
    # 1) Приоритет — явная обёртка "stems".
    explicit = result.get("stems")
    if isinstance(explicit, dict) and explicit:
        return explicit
    # 2) demucs-путь: собрать верхнеуровневые ключи из известного словаря.
    top: dict = {}
    for k, v in result.items():
        url = v.get("url") if isinstance(v, dict) else (v if isinstance(v, str) else None)
        if k in STEM_NAMES and url:
            top[k] = v  # сохраняем исходную форму — _pick_stem ест и {url:..}, и строку
    # 3) Порог >=2 отсекает ложное срабатывание на одиночном ключе-омониме.
    return top if len(top) >= 2 else None


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


def _compact_json(obj: Any) -> str:
    """Компактная сериализация значения для error_message (без утечки структуры в исключение)."""
    try:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(obj)


def _resolve_error_message(data: dict[str, Any], payload: Any) -> str | None:
    """Fallback-цепочка error_message по приоритету.

    Порядок: ``error`` → ``error_message`` → ``payload_error`` → ``payload.detail`` / ``payload``.
    Итог усекается до ``_ERROR_MESSAGE_MAX_LEN``. Если все источники пусты — возвращает None
    (downstream webhooks.py подставит сам нормализованный статус).
    """
    candidate = data.get("error") or data.get("error_message") or data.get("payload_error")
    message: str | None = str(candidate) if candidate else None
    if message is None and isinstance(payload, dict):
        detail = payload.get("detail")
        source = detail if detail is not None else payload
        if source:
            message = _compact_json(source)
    if message is None:
        return None
    return message[:_ERROR_MESSAGE_MAX_LEN]


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

    raw_status = (data.get("status") or "").lower()
    # Уже нормализованные статусы пропускаем как есть; fal-алиасы маппим в существующее множество.
    if raw_status in _WEBHOOK_STATUSES:
        status = raw_status
    elif raw_status in _STATUS_ALIASES:
        status = _STATUS_ALIASES[raw_status]
    else:
        raise WebhookPayloadInvalid(details={"reason": "unknown_status", "status": raw_status})

    payload = data.get("payload")
    # Edge: success-статус (OK/success), но payload пуст и есть payload_error
    # (ошибка сериализации результата на стороне fal) → принудительно failed.
    if status == "completed" and not payload and data.get("payload_error"):
        status = "failed"

    if status == "failed":
        # Для failed медиа не извлекаем — формируем причину по fallback-цепочке.
        media_url, duration_seconds, stems = None, None, None
        error_message = _resolve_error_message(data, payload)
    else:
        # Success-путь: fal queue доставляет результат в конверте; payload — первичный
        # источник, result/output сохранены для обратной совместимости с прямыми форматами.
        result = payload or data.get("result") or data.get("output") or {}
        if not isinstance(result, dict):
            result = {}
        media_url, duration_seconds = extract_media(result)
        stems = extract_stems(result)
        # Для не-failed статусов сохраняем прежнее поведение: верхнеуровневый error/error_message.
        raw_err = data.get("error") or data.get("error_message")
        error_message = str(raw_err) if raw_err else None

    event_id = data.get("event_id") or data.get("eventId") or f"{request_id}:{status}"

    return FalWebhookEvent(
        request_id=str(request_id),
        status=status,
        media_url=media_url,
        duration_seconds=duration_seconds,
        stems=stems,
        error_message=error_message,
        raw=data,
        event_id=str(event_id),
        payload_digest=body_digest(raw_body),
    )
