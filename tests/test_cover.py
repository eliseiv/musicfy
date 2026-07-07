from __future__ import annotations

import pytest

from tests.helpers import (
    auth_headers,
    emit_fal_completed,
    emit_fal_demucs_completed,
    grant_weekly_subscription,
    job_input_payload,
    provider_request_id,
)


async def _coins_available(client, headers) -> int:
    r = await client.get("/v1/billing/balance", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["coinsAvailable"]


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
    """Полный e2e кавера. РЕГРЕСС ADR-008: demucs отдаёт стемы РЕАЛЬНЫМ форматом —
    верхнеуровневыми ключами payload (vocals/drums/bass/other), без обёртки
    ``result["stems"]``. Старый парсер вернул бы stems=None → voice_conversion
    skipped → «no cover audio» (failed). Новый ``extract_stems`` (demucs-путь)
    собирает стемы → cover доходит до completed."""
    headers = await auth_headers(client)
    await grant_weekly_subscription(client, headers)
    coins_before = await _coins_available(client, headers)

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

    # 2. demucs завершён — стемы РЕАЛЬНЫМ форматом (top-level ключи, без "stems")
    rid = await provider_request_id(app, job_id)
    wh1 = await emit_fal_demucs_completed(
        client, rid,
        stems={
            "vocals": "https://cdn.local/vocals.wav",
            "drums": "https://cdn.local/drums.wav",
            "bass": "https://cdn.local/bass.wav",
            "other": "https://cdn.local/other.wav",
        },
    )
    assert wh1.json()["status"] == "ok"

    # 3. стемы распознаны (extract_stems demucs-путь) → перешли к voice_conversion
    mid = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
    assert mid["currentStage"] == "voice_conversion", mid

    # 4. voice-changer завершён — converted vocal
    rid2 = await provider_request_id(app, job_id)
    wh2 = await emit_fal_completed(
        client, rid2, media_url="https://cdn.local/converted_vocal.wav", duration=30.0
    )
    assert wh2.json()["status"] == "ok"

    # 5. cover готов (без ffmpeg в CI — деградация до чистого converted vocal)
    final = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
    assert final["status"] == "completed", final
    track = (await client.get(f"/v1/tracks/{final['trackId']}", headers=headers)).json()
    assert track["kind"] == "cover"
    audio_url = track["variants"][0]["audioUrl"]
    assert audio_url
    # Анти-double-vocal: результат — converted vocal, НЕ source_audio_url (fallback убран).
    assert audio_url == "https://cdn.local/converted_vocal.wav", audio_url
    assert audio_url != "https://cdn.local/input.mp3"
    # cover-трек: prompt отсутствует (meta['prompt'] = None).
    assert track["prompt"] is None, track
    # Монеты списаны (capture) на цену cover (5).
    coins_after = await _coins_available(client, headers)
    assert coins_after == coins_before - 5, (coins_before, coins_after)


@pytest.mark.asyncio
async def test_cover_no_vocal_stem_fails_and_releases_coins(client, app):
    """demucs без вокального стема → voice_conversion skipped → converted_vocal
    отсутствует → «no cover audio» (failed). Зарезервированные монеты возвращаются
    (release), баланс восстанавливается."""
    headers = await auth_headers(client)
    await grant_weekly_subscription(client, headers)
    coins_before = await _coins_available(client, headers)

    resp = await client.post(
        "/v1/covers",
        json={"source_audio_url": "https://cdn.local/input.mp3"},
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["jobId"]

    # demucs вернул только не-вокальные стемы (>=2 ключа, но без vocals/vocal).
    rid = await provider_request_id(app, job_id)
    wh1 = await emit_fal_demucs_completed(
        client, rid,
        stems={
            "drums": "https://cdn.local/drums.wav",
            "bass": "https://cdn.local/bass.wav",
            "other": "https://cdn.local/other.wav",
        },
    )
    assert wh1.json()["status"] == "ok"

    final = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
    assert final["status"] == "failed", final
    assert final["errorMessage"] == "no cover audio", final
    assert final["trackId"] is None
    # Монеты возвращены — баланс как до генерации.
    coins_after = await _coins_available(client, headers)
    assert coins_after == coins_before, (coins_before, coins_after)
    ledger = (await client.get("/v1/billing/ledger", headers=headers)).json()
    assert any(e["kind"] == "credit_release" for e in ledger), ledger


@pytest.mark.asyncio
async def test_cover_degrades_to_clean_vocal_without_ffmpeg(client, app):
    """Без ffmpeg (или без инструментал-стемов) микс невозможен → cover деградирует
    до чистого converted vocal, job succeeded (не failed). НЕ используется
    source_audio_url как инструментал (устранён double-vocal fallback)."""
    headers = await auth_headers(client)
    await grant_weekly_subscription(client, headers)

    resp = await client.post(
        "/v1/covers",
        json={"source_audio_url": "https://cdn.local/original_song.mp3"},
        headers=headers,
    )
    job_id = resp.json()["jobId"]

    # Стемы с вокалом + инструменталом; ffmpeg в CI отсутствует → build_instrumental
    # недоступен, микс пропускается, отдаётся чистый converted vocal.
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
        client, rid2, media_url="https://cdn.local/converted_vocal.wav", duration=25.0
    )

    final = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
    assert final["status"] == "completed", final
    track = (await client.get(f"/v1/tracks/{final['trackId']}", headers=headers)).json()
    audio_url = track["variants"][0]["audioUrl"]
    assert audio_url == "https://cdn.local/converted_vocal.wav", audio_url
    # Ключевой инвариант анти-double-vocal: источник НЕ подставлен как инструментал.
    assert audio_url != "https://cdn.local/original_song.mp3"


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
    """ADR-009: UUID своего ready-клона → 202; payload помечен как clone-ветка
    (`_voice_kind="clone"` + `_target_voice_sample_url` = образец голоса). minimax
    `provider_voice_id` для cover больше НЕ используется — `target_voice` НЕ
    переписывается на него (chatterbox работает с аудио-референсом, а не с id)."""
    headers = await auth_headers(client)
    await grant_weekly_subscription(client, headers)
    profile = await _create_ready_profile(client, headers)
    profile_id = profile["id"]
    minimax_voice_id = profile["providerVoiceId"]
    assert minimax_voice_id, profile

    resp = await client.post(
        "/v1/covers",
        json={"source_audio_url": "https://cdn.local/input.mp3", "targetVoice": profile_id},
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["jobId"]

    payload = await job_input_payload(app, job_id)
    # Клон-ветка: дискриминатор + аудио-образец голоса как референс.
    assert payload.get("_voice_kind") == "clone", payload
    assert payload.get("_target_voice_sample_url") == "https://cdn.local/voice.wav", payload
    # minimax provider_voice_id НЕ подставлен ни в target_voice, ни в референс.
    assert payload.get("_target_voice_sample_url") != minimax_voice_id, payload
    assert payload.get("target_voice") != minimax_voice_id, payload


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
