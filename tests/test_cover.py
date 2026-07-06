from __future__ import annotations

import pytest

from tests.helpers import (
    auth_headers,
    emit_fal_completed,
    grant_weekly_subscription,
    job_input_payload,
    provider_request_id,
)


async def _create_ready_profile(client, headers) -> dict:
    """Создаёт ready voice-профиль текущего пользователя, возвращает тело ответа."""
    consent = await client.post(
        "/v1/voices/consent",
        json={"kind": "own_voice", "accepted": True, "statement": "my voice"},
        headers=headers,
    )
    consent_id = consent.json()["id"]
    resp = await client.post(
        "/v1/voices",
        json={
            "sampleAssetUrl": "https://cdn.local/voice.wav",
            "consentId": consent_id,
            "name": "My Clone",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "ready", body
    return body


@pytest.mark.asyncio
async def test_cover_happy_path_e2e(client, app):
    """Полный e2e кавера по существующему пути (без targetVoice / None)."""
    headers = await auth_headers(client)
    await grant_weekly_subscription(client, headers)

    # 1. создаём cover без targetVoice — существующий путь резолва (None → без изменений)
    resp = await client.post(
        "/v1/covers",
        json={"source_audio_url": "https://cdn.local/input.mp3"},
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["jobId"]

    status = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
    assert status["status"] == "running"
    assert status["currentStage"] == "stem_separation"

    # 2. demucs завершён — возвращает стемы
    rid = await provider_request_id(app, job_id)
    wh1 = await emit_fal_completed(
        client, rid,
        stems={"vocals": "https://cdn.local/vocals.wav", "accompaniment": "https://cdn.local/inst.wav"},
    )
    assert wh1.json()["status"] == "ok"

    # 3. теперь стадия voice_conversion
    mid = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
    assert mid["currentStage"] == "voice_conversion"

    # 4. voice-changer завершён
    rid2 = await provider_request_id(app, job_id)
    wh2 = await emit_fal_completed(
        client, rid2, media_url="https://cdn.local/converted_vocal.wav", duration=30.0
    )
    assert wh2.json()["status"] == "ok"

    # 5. cover готов
    final = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
    assert final["status"] == "completed", final
    track = (await client.get(f"/v1/tracks/{final['trackId']}", headers=headers)).json()
    assert track["kind"] == "cover"
    assert track["variants"][0]["audioUrl"]


@pytest.mark.asyncio
async def test_cover_none_target_voice_accepted(client):
    """Пустой/None targetVoice → 202, payload не переписывается."""
    headers = await auth_headers(client)
    await grant_weekly_subscription(client, headers)
    resp = await client.post(
        "/v1/covers",
        json={"source_audio_url": "https://cdn.local/input.mp3"},
        headers=headers,
    )
    assert resp.status_code == 202, resp.text


@pytest.mark.asyncio
async def test_cover_preset_key_resolves_to_provider_voice(client, app):
    """Валидный пресет-ключ 'aria' → 202; в сохранённом payload target_voice == 'Aria'."""
    headers = await auth_headers(client)
    await grant_weekly_subscription(client, headers)
    resp = await client.post(
        "/v1/covers",
        json={"source_audio_url": "https://cdn.local/input.mp3", "targetVoice": "aria"},
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["jobId"]

    payload = await job_input_payload(app, job_id)
    # Резолв переписал публичный key 'aria' на провайдерский provider_voice 'Aria'.
    assert payload["target_voice"] == "Aria", payload


@pytest.mark.asyncio
async def test_cover_unknown_target_voice_422(client):
    """Неизвестный targetVoice ('english_male') → 422 unknown_voice (ломающее изменение)."""
    headers = await auth_headers(client)
    await grant_weekly_subscription(client, headers)
    resp = await client.post(
        "/v1/covers",
        json={"source_audio_url": "https://cdn.local/input.mp3", "targetVoice": "english_male"},
        headers=headers,
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["details"]["reason"] == "unknown_voice"


@pytest.mark.asyncio
async def test_cover_own_ready_profile_uuid_resolves(client, app):
    """UUID своего ready-профиля → 202; payload переписан на profile.provider_voice_id."""
    headers = await auth_headers(client)
    await grant_weekly_subscription(client, headers)
    profile = await _create_ready_profile(client, headers)
    profile_id = profile["id"]
    expected_voice_id = profile["providerVoiceId"]
    assert expected_voice_id, profile

    resp = await client.post(
        "/v1/covers",
        json={"source_audio_url": "https://cdn.local/input.mp3", "targetVoice": profile_id},
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["jobId"]

    payload = await job_input_payload(app, job_id)
    assert payload["target_voice"] == expected_voice_id, payload


@pytest.mark.asyncio
async def test_cover_foreign_profile_uuid_422(client):
    """UUID чужого профиля → 422 unknown_voice (cross-user isolation)."""
    headers_a = await auth_headers(client)
    profile = await _create_ready_profile(client, headers_a)
    foreign_id = profile["id"]

    headers_b = await auth_headers(client)
    await grant_weekly_subscription(client, headers_b)
    resp = await client.post(
        "/v1/covers",
        json={"source_audio_url": "https://cdn.local/input.mp3", "targetVoice": foreign_id},
        headers=headers_b,
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["details"]["reason"] == "unknown_voice"


@pytest.mark.asyncio
async def test_cover_not_ready_profile_uuid_422(client):
    """UUID собственного не-ready профиля → 422 unknown_voice."""
    # Профиль B, созданный с чужим согласием, остаётся в статусе failed (не ready).
    headers_a = await auth_headers(client)
    consent_a = await client.post(
        "/v1/voices/consent",
        json={"kind": "own_voice", "accepted": True},
        headers=headers_a,
    )
    consent_a_id = consent_a.json()["id"]

    headers_b = await auth_headers(client)
    await grant_weekly_subscription(client, headers_b)
    failed = await client.post(
        "/v1/voices",
        json={"sampleAssetUrl": "https://cdn.local/voice.wav", "consentId": consent_a_id},
        headers=headers_b,
    )
    assert failed.status_code == 201
    assert failed.json()["status"] == "failed"
    not_ready_id = failed.json()["id"]

    resp = await client.post(
        "/v1/covers",
        json={"source_audio_url": "https://cdn.local/input.mp3", "targetVoice": not_ready_id},
        headers=headers_b,
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["details"]["reason"] == "unknown_voice"


@pytest.mark.asyncio
async def test_cover_requires_credits(client):
    headers = await auth_headers(client)
    resp = await client.post(
        "/v1/covers", json={"source_audio_url": "https://cdn.local/in.mp3"}, headers=headers
    )
    assert resp.status_code == 402
    assert resp.json()["error"]["code"] == "INSUFFICIENT_CREDITS"
