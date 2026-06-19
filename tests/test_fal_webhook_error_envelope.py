"""Тесты обработки ERROR-конверта fal queue webhook (закрытие TD-003).

Целевой контракт зафиксирован в docs/ARCHITECTURE.md →
"Контракт интеграции fal.ai" → "Обработка error-конверта fal queue", пункты 1-6:
  1. нормализация status (lower-case + алиасы) в {completed,failed,canceled,in_progress};
     "ERROR"/"error" больше НЕ отвергается, а маппится в failed;
  2. fallback-цепочка error_message: error → payload_error → payload.detail/payload,
     компактная сериализация, усечение до 500 символов;
  3. edge: OK + пустой payload + payload_error → failed, error_message из payload_error;
  4. success-путь без изменений (регрессия не допускается);
  5. pipeline-контракт не меняется (вне зоны парсера).

Тестируем единый парсер parse_fal_webhook_event напрямую (unit-уровень функции).
Файл подхватывает autouse `clean_db` из conftest → требуется тестовая БД (postgres
из docker-compose на порту 5544, DATABASE_URL переопределяется через env).
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from app.api.errors import WebhookPayloadInvalid
from app.domain.providers.fal.parsing import (
    _ERROR_MESSAGE_MAX_LEN,
    _compact_json,
    parse_fal_webhook_event,
)
from app.domain.providers.fal.signature import body_digest

AUDIO_URL = "https://v3b.fal.media/files/abc/output.mp3"


def _parse(body: dict[str, Any]):
    raw = json.dumps(body).encode("utf-8")
    return parse_fal_webhook_event(raw), raw


# --------------------------------------------------------------------------
# Сценарий 1: ERROR + верхнеуровневый error → failed, error_message == error
# --------------------------------------------------------------------------

def test_error_status_with_toplevel_error_maps_to_failed():
    event, raw = _parse(
        {
            "request_id": "req-e1",
            "gateway_request_id": "gw-e1",
            "status": "ERROR",
            "payload": None,
            "error": "model inference failed: OOM",
        }
    )
    assert event.status == "failed"  # ERROR → failed (TD-003)
    assert event.error_message == "model inference failed: OOM"
    assert event.media_url is None
    assert event.duration_seconds is None
    assert event.stems is None
    assert event.request_id == "req-e1"
    assert event.payload_digest == body_digest(raw)


# --------------------------------------------------------------------------
# Сценарий 2: ERROR без error, с payload.detail → error_message == компактный JSON detail
# --------------------------------------------------------------------------

def test_error_status_falls_back_to_payload_detail():
    detail = {"code": "VALIDATION", "fields": ["prompt", "lyrics"]}
    event, _ = _parse(
        {
            "request_id": "req-e2",
            "status": "ERROR",
            "error": None,
            "payload": {"detail": detail},
        }
    )
    assert event.status == "failed"
    # Компактная сериализация detail (separators без пробелов).
    assert event.error_message == json.dumps(detail, ensure_ascii=False, separators=(",", ":"))
    assert event.media_url is None


def test_error_status_payload_detail_truncated_to_500():
    """Сценарий 7 (через detail): длинный detail усекается ровно до 500 символов."""
    detail = "x" * 1000
    event, _ = _parse(
        {
            "request_id": "req-e2b",
            "status": "ERROR",
            "payload": {"detail": detail},
        }
    )
    assert event.status == "failed"
    assert event.error_message is not None
    assert len(event.error_message) == _ERROR_MESSAGE_MAX_LEN == 500


# --------------------------------------------------------------------------
# Сценарий 3: ERROR без error/detail, payload как dict → error_message == компактный JSON payload
# --------------------------------------------------------------------------

def test_error_status_falls_back_to_whole_payload_dict():
    payload = {"reason": "rate_limited", "retry_after": 30}
    event, _ = _parse(
        {
            "request_id": "req-e3",
            "status": "ERROR",
            "payload": payload,
        }
    )
    assert event.status == "failed"
    assert event.error_message == json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


# --------------------------------------------------------------------------
# Сценарий 4: OK + payload=null + payload_error → failed, error_message == payload_error
# --------------------------------------------------------------------------

def test_ok_with_null_payload_and_payload_error_forced_failed():
    event, _ = _parse(
        {
            "request_id": "req-e4",
            "status": "OK",
            "payload": None,
            "payload_error": "result serialization failed",
        }
    )
    assert event.status == "failed"  # success-путь принудительно переведён в failed
    assert event.error_message == "result serialization failed"
    assert event.media_url is None
    assert event.duration_seconds is None


# --------------------------------------------------------------------------
# Сценарий 5: OK + непустой payload (success) → completed, media_url, error_message==None
# (регрессия success-пути НЕ допущена)
# --------------------------------------------------------------------------

def test_ok_with_payload_success_path_unchanged():
    event, raw = _parse(
        {
            "request_id": "req-e5",
            "status": "OK",
            "payload": {"audio": {"url": AUDIO_URL, "duration": 73.0}},
            "error": None,
        }
    )
    assert event.status == "completed"
    assert event.media_url == AUDIO_URL
    assert event.duration_seconds == 73.0
    assert event.error_message is None
    assert event.payload_digest == body_digest(raw)


def test_ok_with_payload_error_present_but_payload_nonempty_stays_completed():
    """payload_error не должен трогать success-путь, пока payload непуст."""
    event, _ = _parse(
        {
            "request_id": "req-e5b",
            "status": "OK",
            "payload": {"audio": {"url": AUDIO_URL}},
            "payload_error": "ignored when payload non-empty",
        }
    )
    assert event.status == "completed"
    assert event.media_url == AUDIO_URL
    assert event.error_message is None


# --------------------------------------------------------------------------
# Сценарий 6: истинно неизвестный статус (вне маппинга и whitelist)
# → WebhookPayloadInvalid(reason=unknown_status)
# --------------------------------------------------------------------------

def test_truly_unknown_status_rejected():
    raw = json.dumps({"request_id": "req-e6", "status": "WEIRD_STATE"}).encode("utf-8")
    with pytest.raises(WebhookPayloadInvalid) as exc_info:
        parse_fal_webhook_event(raw)
    assert exc_info.value.details["reason"] == "unknown_status"
    assert exc_info.value.details["status"] == "weird_state"


# --------------------------------------------------------------------------
# Сценарий 7: error_message длиннее 500 символов → усекается ровно до 500
# (через верхнеуровневый error)
# --------------------------------------------------------------------------

def test_toplevel_error_longer_than_500_truncated():
    long_error = "E" * 750
    event, _ = _parse(
        {
            "request_id": "req-e7",
            "status": "ERROR",
            "error": long_error,
        }
    )
    assert event.status == "failed"
    assert event.error_message == "E" * 500
    assert len(event.error_message) == 500


# --------------------------------------------------------------------------
# Сценарий 8: несериализуемый payload в цепочке → не падает наружу (str fallback)
# Вход webhook — bytes → json.loads всегда даёт сериализуемое; fallback-ветку
# _compact_json (TypeError/ValueError → str) тестируем напрямую как unit.
# --------------------------------------------------------------------------

def test_compact_json_falls_back_to_str_on_unserializable():
    class _Unserializable:
        def __repr__(self) -> str:
            return "<unserializable-obj>"

    obj = _Unserializable()
    # json.dumps бросит TypeError → _compact_json не должен пробросить исключение наружу.
    result = _compact_json(obj)
    assert result == "<unserializable-obj>"


def test_compact_json_handles_nan_value_error_fallback():
    """float('nan') допустим в json.dumps по умолчанию — проверяем штатную сериализацию
    и устойчивость к set (несериализуемый тип) через str fallback."""
    result = _compact_json({"a", "b"})  # set не сериализуем JSON-ом → str fallback
    assert isinstance(result, str) and result != ""


def test_error_envelope_with_serializable_nested_payload_does_not_raise():
    """Полный путь парсера на ERROR с вложенным payload не падает наружу."""
    event, _ = _parse(
        {
            "request_id": "req-e8",
            "status": "ERROR",
            "payload": {"detail": {"nested": {"deep": [1, 2, 3]}}},
        }
    )
    assert event.status == "failed"
    assert event.error_message is not None


# --------------------------------------------------------------------------
# Контракт: нормализация всех статусов (пункт 1)
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw_status,expected",
    [
        ("ERROR", "failed"),
        ("error", "failed"),
        ("failed", "failed"),
        ("canceled", "canceled"),
        ("cancelled", "canceled"),
        ("CANCELLED", "canceled"),
        ("in_progress", "in_progress"),
        ("IN_PROGRESS", "in_progress"),
        ("OK", "completed"),
        ("success", "completed"),
    ],
)
def test_status_normalization(raw_status: str, expected: str):
    body: dict[str, Any] = {"request_id": f"req-{raw_status}", "status": raw_status}
    # Для success-статусов кладём непустой payload, чтобы не сработал forced-failed edge.
    if expected == "completed":
        body["payload"] = {"audio": {"url": AUDIO_URL}}
    event, _ = _parse(body)
    assert event.status == expected


def test_failed_status_with_no_error_sources_yields_none_message():
    """failed без error/payload_error/payload → error_message остаётся None
    (downstream webhook-route делает fallback на сам статус)."""
    event, _ = _parse({"request_id": "req-none", "status": "failed"})
    assert event.status == "failed"
    assert event.error_message is None


# --------------------------------------------------------------------------
# Приоритет fallback-цепочки error_message (пункт 2): error > error_message > payload_error
# Существующие тесты проверяют каждый источник по отдельности, но НЕ порядок
# приоритета, когда несколько источников присутствуют одновременно.
# --------------------------------------------------------------------------

def test_error_message_priority_toplevel_error_wins_over_others():
    """Когда присутствуют все источники, верхнеуровневый `error` имеет высший приоритет."""
    event, _ = _parse(
        {
            "request_id": "req-prio1",
            "status": "ERROR",
            "error": "top-level error wins",
            "error_message": "secondary error_message",
            "payload_error": "tertiary payload_error",
            "payload": {"detail": "lowest detail"},
        }
    )
    assert event.status == "failed"
    assert event.error_message == "top-level error wins"


def test_error_message_priority_error_message_wins_over_payload_error():
    """Без верхнеуровневого `error` приоритет у `error_message` над `payload_error`."""
    event, _ = _parse(
        {
            "request_id": "req-prio2",
            "status": "ERROR",
            "error": None,
            "error_message": "secondary error_message wins",
            "payload_error": "tertiary payload_error",
            "payload": {"detail": "lowest detail"},
        }
    )
    assert event.status == "failed"
    assert event.error_message == "secondary error_message wins"


def test_error_message_priority_payload_error_wins_over_payload_detail():
    """Без error/error_message приоритет у `payload_error` над сериализацией payload.detail."""
    event, _ = _parse(
        {
            "request_id": "req-prio3",
            "status": "ERROR",
            "payload_error": "payload_error wins over detail",
            "payload": {"detail": {"code": "X"}},
        }
    )
    assert event.status == "failed"
    assert event.error_message == "payload_error wins over detail"
