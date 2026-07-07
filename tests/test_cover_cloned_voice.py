"""ADR-009 — cover с клонированным голосом через chatterbox speech-to-speech.

Клон-ветка cover-конвертации идёт через `submit_speech_to_speech`
(`fal-ai/chatterbox/speech-to-speech`) с образцом голоса (`sample_asset_url`) как
аудио-референсом. Пресет-ветка остаётся на ElevenLabs `submit_voice_changer`
(ADR-006, регрессия). Дискриминатор — `_voice_kind` в payload, выставляемый
`_resolve_target_voice`.

Все тесты — на StubFalProvider (реальный fal не вызывается). Пайплайн продвигается
webhook'ами через общий `parse_fal_webhook_event`, поэтому форма результата стаба
и реального fal физически не расходятся.
"""

from __future__ import annotations

import inspect
import uuid as _uuid

import pytest

from app.domain.models.voice import VoiceProfile
from app.domain.providers.fal.stub import StubFalProvider
from tests.helpers import (
    auth_headers,
    emit_fal_completed,
    emit_fal_demucs_completed,
    emit_fal_error,
    grant_weekly_subscription,
    provider_request_id,
)

CLONE_SAMPLE_URL = "https://cdn.local/voice.wav"


class _FalSpy:
    """Оборачивает clone/preset submit-методы провайдера, записывая аргументы вызова.

    Делегирует оригинальным реализациям (стаб), поэтому пайплайн работает как обычно.
    Методы провайдера keyword-only → перехват через **kwargs корректен.
    """

    def __init__(self, provider) -> None:
        self.speech_to_speech: list[dict] = []
        self.voice_changer: list[dict] = []
        _orig_sts = provider.submit_speech_to_speech
        _orig_vc = provider.submit_voice_changer

        async def sts(**kwargs):
            self.speech_to_speech.append(kwargs)
            return await _orig_sts(**kwargs)

        async def vc(**kwargs):
            self.voice_changer.append(kwargs)
            return await _orig_vc(**kwargs)

        provider.submit_speech_to_speech = sts
        provider.submit_voice_changer = vc


async def _create_ready_clone(client, headers) -> dict:
    """Создаёт ready voice-клон текущего пользователя (sample = CLONE_SAMPLE_URL)."""
    consent = await client.post(
        "/v1/voices/consent",
        json={"kind": "own_voice", "accepted": True, "statement": "my voice"},
        headers=headers,
    )
    consent_id = consent.json()["id"]
    resp = await client.post(
        "/v1/voices",
        json={"sampleAssetUrl": CLONE_SAMPLE_URL, "consentId": consent_id, "name": "My Clone"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "ready", body
    return body


async def _coins_available(client, headers) -> int:
    r = await client.get("/v1/billing/balance", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["coinsAvailable"]


# --------------------------------------------------------------------------- #
# 1. Клон-ветка → chatterbox speech-to-speech (e2e до completed)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cover_clone_uses_speech_to_speech_e2e(client, app):
    """Cover со своим ready-клоном: job доходит до completed; вызван
    `submit_speech_to_speech` (chatterbox) с target_voice_audio_url = sample клона;
    `submit_voice_changer` (ElevenLabs) НЕ вызван; minimax provider_voice_id в fal
    не уходит."""
    headers = await auth_headers(client)
    await grant_weekly_subscription(client, headers)
    profile = await _create_ready_clone(client, headers)
    minimax_voice_id = profile["providerVoiceId"]

    spy = _FalSpy(app.state.fal_provider)

    resp = await client.post(
        "/v1/covers",
        json={"source_audio_url": "https://cdn.local/input.mp3", "targetVoice": profile["id"]},
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["jobId"]

    # demucs → стемы (вокал + инструментал) реальным top-level форматом (ADR-008).
    rid = await provider_request_id(app, job_id)
    await emit_fal_demucs_completed(
        client, rid,
        stems={
            "vocals": "https://cdn.local/vocals.wav",
            "other": "https://cdn.local/other.wav",
        },
    )

    # Клон-ветка: вызван speech_to_speech, НЕ voice_changer.
    assert len(spy.speech_to_speech) == 1, spy.speech_to_speech
    assert spy.voice_changer == [], spy.voice_changer
    call = spy.speech_to_speech[0]
    assert call["source_audio_url"] == "https://cdn.local/vocals.wav", call
    assert call["target_voice_audio_url"] == CLONE_SAMPLE_URL, call
    # minimax id нигде не участвует в fal-вызове.
    assert call["target_voice_audio_url"] != minimax_voice_id, call
    assert minimax_voice_id not in call.values(), call
    assert call["idempotency_key"] == f"{job_id}:vc", call

    # chatterbox завершил конверсию → converted vocal → cover completed.
    rid2 = await provider_request_id(app, job_id)
    await emit_fal_completed(
        client, rid2, media_url="https://cdn.local/clone_converted.wav", duration=28.0
    )
    final = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
    assert final["status"] == "completed", final
    track = (await client.get(f"/v1/tracks/{final['trackId']}", headers=headers)).json()
    assert track["kind"] == "cover"
    assert track["variants"][0]["audioUrl"] == "https://cdn.local/clone_converted.wav"


# --------------------------------------------------------------------------- #
# 2. Пресет-ветка → ElevenLabs voice-changer (регрессия ADR-006)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cover_preset_uses_voice_changer_regression(client, app):
    """Пресет 'aria': конверсия идёт через `submit_voice_changer` (ElevenLabs) с
    voice='Aria'; chatterbox НЕ вызван; job доходит до completed."""
    headers = await auth_headers(client)
    await grant_weekly_subscription(client, headers)

    spy = _FalSpy(app.state.fal_provider)

    resp = await client.post(
        "/v1/covers",
        json={"source_audio_url": "https://cdn.local/input.mp3", "targetVoice": "aria"},
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["jobId"]

    rid = await provider_request_id(app, job_id)
    await emit_fal_demucs_completed(
        client, rid,
        stems={
            "vocals": "https://cdn.local/vocals.wav",
            "other": "https://cdn.local/other.wav",
        },
    )

    # Пресет-ветка: voice_changer с provider_voice 'Aria', speech_to_speech НЕ вызван.
    assert len(spy.voice_changer) == 1, spy.voice_changer
    assert spy.speech_to_speech == [], spy.speech_to_speech
    vc_call = spy.voice_changer[0]
    assert vc_call["target_voice"] == "Aria", vc_call
    assert vc_call["audio_url"] == "https://cdn.local/vocals.wav", vc_call

    rid2 = await provider_request_id(app, job_id)
    await emit_fal_completed(
        client, rid2, media_url="https://cdn.local/preset_converted.wav", duration=22.0
    )
    final = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
    assert final["status"] == "completed", final


# --------------------------------------------------------------------------- #
# 3. Без targetVoice → дефолтная voice-changer ветка (target_voice='default')
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cover_no_target_voice_default_changer(client, app):
    """Без targetVoice: дефолтная ветка voice_changer с target_voice='default';
    chatterbox НЕ вызван; completed."""
    headers = await auth_headers(client)
    await grant_weekly_subscription(client, headers)

    spy = _FalSpy(app.state.fal_provider)

    resp = await client.post(
        "/v1/covers",
        json={"source_audio_url": "https://cdn.local/input.mp3"},
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["jobId"]

    rid = await provider_request_id(app, job_id)
    await emit_fal_demucs_completed(
        client, rid,
        stems={
            "vocals": "https://cdn.local/vocals.wav",
            "other": "https://cdn.local/other.wav",
        },
    )

    assert len(spy.voice_changer) == 1, spy.voice_changer
    assert spy.speech_to_speech == [], spy.speech_to_speech
    assert spy.voice_changer[0]["target_voice"] == "default", spy.voice_changer[0]

    rid2 = await provider_request_id(app, job_id)
    await emit_fal_completed(
        client, rid2, media_url="https://cdn.local/default_converted.wav", duration=20.0
    )
    final = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
    assert final["status"] == "completed", final


# --------------------------------------------------------------------------- #
# 4. Defensive 422: ready-клон с пустым sample_asset_url → unknown_voice
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cover_clone_empty_sample_422_no_reserve(client, app):
    """Ready-клон с пустым sample_asset_url → 422 unknown_voice ДО резерва: монеты
    не списаны/не зарезервированы (проверка происходит до создания job)."""
    headers = await auth_headers(client)
    await grant_weekly_subscription(client, headers)
    profile = await _create_ready_clone(client, headers)
    profile_id = profile["id"]
    coins_before = await _coins_available(client, headers)

    # Обнуляем образец голоса у ready-профиля (эмуляция дефекта данных).
    async with app.state.sessionmaker() as session:
        async with session.begin():
            prof = await session.get(VoiceProfile, _uuid.UUID(profile_id))
            prof.sample_asset_url = ""

    resp = await client.post(
        "/v1/covers",
        json={"source_audio_url": "https://cdn.local/input.mp3", "targetVoice": profile_id},
        headers=headers,
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["details"]["reason"] == "unknown_voice"
    # Монеты не тронуты — резерв не выполнялся.
    assert await _coins_available(client, headers) == coins_before


# --------------------------------------------------------------------------- #
# 5a. Инвариант монет: capture при успехе клон-cover (списание цены cover)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cover_clone_success_captures_coins(client, app):
    """Успешный клон-cover списывает монеты (capture) на цену cover (5)."""
    headers = await auth_headers(client)
    await grant_weekly_subscription(client, headers)
    profile = await _create_ready_clone(client, headers)
    coins_before = await _coins_available(client, headers)

    resp = await client.post(
        "/v1/covers",
        json={"source_audio_url": "https://cdn.local/input.mp3", "targetVoice": profile["id"]},
        headers=headers,
    )
    job_id = resp.json()["jobId"]

    rid = await provider_request_id(app, job_id)
    await emit_fal_demucs_completed(
        client, rid,
        stems={
            "vocals": "https://cdn.local/vocals.wav",
            "other": "https://cdn.local/other.wav",
        },
    )
    rid2 = await provider_request_id(app, job_id)
    await emit_fal_completed(
        client, rid2, media_url="https://cdn.local/clone_converted.wav", duration=27.0
    )

    final = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
    assert final["status"] == "completed", final
    assert await _coins_available(client, headers) == coins_before - 5


# --------------------------------------------------------------------------- #
# 5b. Инвариант монет: release при провале клон-конверсии
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cover_clone_conversion_error_releases_coins(client, app):
    """Провал chatterbox-конверсии (ERROR-webhook) → job failed → зарезервированные
    монеты возвращаются (release); баланс восстанавливается, есть credit_release."""
    headers = await auth_headers(client)
    await grant_weekly_subscription(client, headers)
    profile = await _create_ready_clone(client, headers)
    coins_before = await _coins_available(client, headers)

    resp = await client.post(
        "/v1/covers",
        json={"source_audio_url": "https://cdn.local/input.mp3", "targetVoice": profile["id"]},
        headers=headers,
    )
    job_id = resp.json()["jobId"]

    rid = await provider_request_id(app, job_id)
    await emit_fal_demucs_completed(
        client, rid,
        stems={
            "vocals": "https://cdn.local/vocals.wav",
            "other": "https://cdn.local/other.wav",
        },
    )
    # chatterbox-стадия падает.
    rid2 = await provider_request_id(app, job_id)
    await emit_fal_error(client, rid2, error="chatterbox inference failed")

    final = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
    assert final["status"] == "failed", final
    assert await _coins_available(client, headers) == coins_before
    ledger = (await client.get("/v1/billing/ledger", headers=headers)).json()
    assert any(e["kind"] == "credit_release" for e in ledger), ledger


# --------------------------------------------------------------------------- #
# 6. Protocol conformance: StubFalProvider реализует submit_speech_to_speech
# --------------------------------------------------------------------------- #
def test_stub_implements_speech_to_speech_signature():
    """StubFalProvider объявляет submit_speech_to_speech с ABC-совместимой
    keyword-only сигнатурой (ADR-009 §4)."""
    method = getattr(StubFalProvider, "submit_speech_to_speech", None)
    assert method is not None and callable(method)
    params = inspect.signature(method).parameters
    for name in ("source_audio_url", "target_voice_audio_url", "webhook_url", "idempotency_key"):
        assert name in params, params
        assert params[name].kind == inspect.Parameter.KEYWORD_ONLY, (name, params[name].kind)
