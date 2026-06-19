"""Тесты фикса парсинга webhook/poll от fal.ai (minimax-music/v2.6).

fal queue доставляет webhook в конверте:
  {"request_id":..,"status":"OK","payload":{<результат модели>},"error":null}
а реальный результат модели — {"audio":{"url":..,"duration":..}} внутри payload.

parse_webhook_event синхронный (event loop не нужен) — но файл всё равно
подхватывает autouse `clean_db` из conftest (требуется тестовая БД).
fetch_status async — httpx.AsyncClient мокается напрямую через provider._client.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.errors import WebhookPayloadInvalid
from app.domain.providers.fal.client import FalAiProvider
from app.domain.providers.fal.signature import body_digest
from app.domain.providers.fal.stub import StubFalProvider

AUDIO_URL = "https://v3b.fal.media/files/abc/output.mp3"


def _make_provider() -> FalAiProvider:
    return FalAiProvider(
        api_key="test-key",
        base_url="https://queue.fal.run",
        song_model="fal-ai/minimax-music/v2.6",
        refine_model="fal-ai/refine",
        speech_model="fal-ai/speech",
        voice_clone_model="fal-ai/voice-clone",
        lyrics_llm="fal-ai/llm",
        demucs_model="fal-ai/demucs",
        voice_changer_model="fal-ai/voice-changer",
        video_model="fal-ai/video",
        webhook_secret="test-webhook-secret",
    )


def _parse(provider: FalAiProvider, body: dict[str, Any]):
    raw = json.dumps(body).encode("utf-8")
    return provider.parse_webhook_event(headers={}, raw_body=raw), raw


# --------------------------------------------------------------------------
# parse_webhook_event (синхронный)
# --------------------------------------------------------------------------

def test_parse_webhook_fal_envelope_completed_extracts_media():
    """Сценарий 1: реальный fal-конверт с payload.audio → media_url/duration/completed."""
    provider = _make_provider()
    body = {
        "request_id": "req-1",
        "gateway_request_id": "gw-1",
        "status": "OK",
        "payload": {"audio": {"url": AUDIO_URL, "duration": 123.5, "file_size": 4805829}},
        "error": None,
    }
    event, raw = _parse(provider, body)
    assert event.status == "completed"  # OK → completed
    assert event.media_url == AUDIO_URL
    assert event.duration_seconds == 123.5
    assert event.request_id == "req-1"
    assert event.error_message is None
    assert event.payload_digest == body_digest(raw)


def test_parse_webhook_backcompat_result_key_extracts_media():
    """Сценарий 2: payload отсутствует, есть result.audio → media_url (старый формат)."""
    provider = _make_provider()
    body = {
        "request_id": "req-2",
        "status": "completed",
        "result": {"audio": {"url": AUDIO_URL}},
    }
    event, _ = _parse(provider, body)
    assert event.status == "completed"
    assert event.media_url == AUDIO_URL


def test_parse_webhook_backcompat_output_key_extracts_media():
    """Сценарий 3: output.audio → media_url (иной legacy-формат)."""
    provider = _make_provider()
    body = {
        "request_id": "req-3",
        "status": "completed",
        "output": {"audio": {"url": AUDIO_URL}},
    }
    event, _ = _parse(provider, body)
    assert event.status == "completed"
    assert event.media_url == AUDIO_URL


def test_parse_webhook_stems_unpacked_from_payload():
    """Сценарий 4: payload.stems → event.stems равен этому dict."""
    provider = _make_provider()
    stems = {"vocal": "https://v3b.fal.media/vocal.mp3", "music": "https://v3b.fal.media/music.mp3"}
    body = {
        "request_id": "req-4",
        "status": "OK",
        "payload": {"audio": {"url": AUDIO_URL}, "stems": stems},
    }
    event, _ = _parse(provider, body)
    assert event.stems == stems


def test_parse_webhook_failed_keeps_error_message_from_envelope():
    """Сценарий 5: status=failed + error на верхнем уровне конверта → error_message сохранён."""
    provider = _make_provider()
    body = {
        "request_id": "req-5",
        "status": "failed",
        "payload": None,
        "error": "model inference error",
    }
    event, _ = _parse(provider, body)
    assert event.status == "failed"
    assert event.error_message == "model inference error"
    assert event.media_url is None


def test_parse_webhook_error_status_maps_to_failed():
    """Контракт TD-003: сырой 'ERROR' больше НЕ отвергается, а маппится в failed
    (см. ARCHITECTURE.md → Обработка error-конверта fal queue, п.1). Подробные
    сценарии error-конверта — в tests/test_fal_webhook_error_envelope.py."""
    provider = _make_provider()
    raw = json.dumps(
        {"request_id": "req-x", "status": "ERROR", "error": "boom"}
    ).encode("utf-8")
    event = provider.parse_webhook_event(headers={}, raw_body=raw)
    assert event.status == "failed"
    assert event.error_message == "boom"


def test_parse_webhook_truly_unknown_status_rejected():
    """Статусы вне множества алиасов по-прежнему отвергаются как WebhookPayloadInvalid."""
    provider = _make_provider()
    raw = json.dumps({"request_id": "req-x", "status": "WEIRD"}).encode("utf-8")
    with pytest.raises(WebhookPayloadInvalid):
        provider.parse_webhook_event(headers={}, raw_body=raw)


# --------------------------------------------------------------------------
# Контракт: оба провайдера парсят конверт ИДЕНТИЧНО (единый parse_fal_webhook_event)
# --------------------------------------------------------------------------

def test_both_providers_parse_real_envelope_identically():
    """Реальный fal-конверт даёт корректный media_url, и FalAiProvider и
    StubFalProvider возвращают побитово совпадающее событие — они физически
    не могут разойтись в трактовке контракта (общий parse_fal_webhook_event)."""
    real = _make_provider()
    stub = StubFalProvider(webhook_secret="test-webhook-secret")
    body = {
        "request_id": "req-shared",
        "gateway_request_id": "gw-shared",
        "status": "OK",
        "payload": {
            "audio": {"url": AUDIO_URL, "duration": 88.0},
            "stems": {"vocal": "https://v3b.fal.media/v.mp3"},
        },
        "error": None,
    }
    raw = json.dumps(body).encode("utf-8")

    real_event = real.parse_webhook_event(headers={}, raw_body=raw)
    stub_event = stub.parse_webhook_event(headers={}, raw_body=raw)

    # Корректный разбор реального конверта.
    assert real_event.status == "completed"
    assert real_event.media_url == AUDIO_URL
    assert real_event.duration_seconds == 88.0
    assert real_event.payload_digest == body_digest(raw)

    # Идентичность обоих провайдеров по всем полям нормализованного события.
    assert real_event == stub_event


# --------------------------------------------------------------------------
# fetch_status (async, poll-путь) — мокаем provider._client
# --------------------------------------------------------------------------

def _resp(status_code: int, json_body: Any) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.json = MagicMock(return_value=json_body)
    r.text = json.dumps(json_body)
    return r


async def test_fetch_status_direct_result_completed_extracts_media():
    """Сценарий 6: result-эндпоинт отдаёт прямой {"audio":{url}} без конверта (регресс)."""
    provider = _make_provider()
    provider._client = MagicMock()
    provider._client.get = AsyncMock(
        side_effect=[
            _resp(200, {"status": "COMPLETED"}),
            _resp(200, {"audio": {"url": AUDIO_URL, "duration": 60.0}}),
        ]
    )
    result = await provider.fetch_status(
        model="fal-ai/minimax-music/v2.6",
        request_id="req-6",
        status_url="https://queue.fal.run/x/status",
        response_url="https://queue.fal.run/x",
    )
    assert result.status == "COMPLETED"
    assert result.media_url == AUDIO_URL
    assert result.duration_seconds == 60.0


async def test_fetch_status_envelope_payload_unpacked():
    """Сценарий 7: result-эндпоинт отдал конверт с payload-dict → распаковка работает."""
    provider = _make_provider()
    stems = {"vocal": "https://v3b.fal.media/v.mp3"}
    provider._client = MagicMock()
    provider._client.get = AsyncMock(
        side_effect=[
            _resp(200, {"status": "COMPLETED"}),
            _resp(
                200,
                {
                    "request_id": "req-7",
                    "status": "OK",
                    "payload": {"audio": {"url": AUDIO_URL, "duration": 42.0}, "stems": stems},
                },
            ),
        ]
    )
    result = await provider.fetch_status(
        model="fal-ai/minimax-music/v2.6",
        request_id="req-7",
        status_url="https://queue.fal.run/x/status",
        response_url="https://queue.fal.run/x",
    )
    assert result.status == "COMPLETED"
    assert result.media_url == AUDIO_URL
    assert result.duration_seconds == 42.0
    assert result.stems == stems
