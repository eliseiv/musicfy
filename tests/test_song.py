from __future__ import annotations

import json

import pytest

from app.domain.models.job import Job
from app.domain.providers.fal.signature import compute_signature
from tests.helpers import auth_headers, grant_weekly_subscription

WEBHOOK_SECRET = "test-webhook-secret"


async def _auth(client) -> dict:
    """Auth + подписка (чтобы хватало кредитов на генерацию)."""
    headers = await auth_headers(client)
    await grant_weekly_subscription(client, headers)
    return headers


async def _provider_request_id(app, job_id: str) -> str:
    async with app.state.sessionmaker() as session:
        job = await session.get(Job, __import__("uuid").UUID(job_id))
        return job.provider_request_id


async def _emit_fal_completed(client, request_id: str, *, media_url: str, duration: float):
    # Реальный fal queue webhook: конверт {request_id,status,payload,error},
    # payload = результат модели {"audio": {"url": .., "duration": ..}}.
    body = json.dumps(
        {
            "request_id": request_id,
            "status": "OK",
            "payload": {"audio": {"url": media_url, "duration": duration}},
            "error": None,
        }
    ).encode("utf-8")
    sig = compute_signature(WEBHOOK_SECRET, body)
    return await client.post(
        "/v1/webhooks/fal",
        content=body,
        headers={"X-Fal-Signature": sig, "Content-Type": "application/json"},
    )


async def _emit_fal_error(client, request_id: str, *, error: str = "model inference failed"):
    # Реальный fal queue ERROR-конверт (TD-003): {request_id,status:"ERROR",error,payload}.
    # Парсер маппит ERROR → failed; webhook-route переводит job в failed и делает refund.
    body = json.dumps(
        {
            "request_id": request_id,
            "status": "ERROR",
            "error": error,
            "payload": {"detail": [{"loc": ["body"], "msg": "invalid"}]},
        }
    ).encode("utf-8")
    sig = compute_signature(WEBHOOK_SECRET, body)
    return await client.post(
        "/v1/webhooks/fal",
        content=body,
        headers={"X-Fal-Signature": sig, "Content-Type": "application/json"},
    )


@pytest.mark.asyncio
async def test_presets_seeded(client):
    genres = await client.get("/v1/presets/genres")
    assert genres.status_code == 200
    assert any(g["key"] == "pop" for g in genres.json())
    moods = await client.get("/v1/presets/moods")
    assert any(m["key"] == "chill" for m in moods.json())


@pytest.mark.asyncio
async def test_lyrics_generate_and_edit(client):
    headers = await _auth(client)
    gen = await client.post(
        "/v1/lyrics", json={"prompt": "song about the ocean", "language": "en"}, headers=headers
    )
    assert gen.status_code == 200, gen.text
    draft = gen.json()
    assert draft["content"]
    assert draft["source"] == "generated"

    edited = await client.patch(
        f"/v1/lyrics/{draft['id']}", json={"content": "my edited lyrics"}, headers=headers
    )
    assert edited.status_code == 200
    assert edited.json()["content"] == "my edited lyrics"
    assert edited.json()["source"] == "edited"


@pytest.mark.asyncio
async def test_song_happy_path_e2e(client, app):
    headers = await _auth(client)

    # 1. создаём песню
    resp = await client.post(
        "/v1/songs",
        json={"prompt": "an upbeat indie pop song", "genre": "pop", "mood": "happy"},
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["jobId"]

    # 2. джоба запущена (running), стадия music_generation
    status = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
    assert status["status"] == "running"
    assert status["currentStage"] == "music_generation"

    # 3. эмулируем webhook завершения music_generation
    rid = await _provider_request_id(app, job_id)
    wh = await _emit_fal_completed(
        client, rid, media_url="https://cdn.local/song.mp3", duration=42.0
    )
    assert wh.status_code == 200, wh.text
    assert wh.json()["status"] == "ok"

    # 4. джоба завершена, есть трек
    final = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
    assert final["status"] == "completed", final
    assert final["trackId"]

    track = (await client.get(f"/v1/tracks/{final['trackId']}", headers=headers)).json()
    assert track["kind"] == "song"
    assert len(track["variants"]) == 1
    assert track["variants"][0]["audioUrl"] == "https://cdn.local/song.mp3"
    assert track["variants"][0]["durationSeconds"] == 42.0


@pytest.mark.asyncio
async def test_song_error_envelope_fails_job_and_refunds(client, app):
    """TD-003 интеграция: ERROR-конверт fal по реальному jobId → job в failed
    с error_code=PROVIDER_FAILED, error_message из верхнеуровневого `error`,
    и возврат зарезервированных кредитов (credit_release в журнале).

    Song-пайплайн на первой ошибке music_generation делает music-fallback
    (повторная submit другой модели — see song.py::_try_music_fallback),
    поэтому терминального failed добиваемся вторым ERROR-конвертом по уже
    обновлённому request_id fallback-стадии."""
    headers = await _auth(client)

    # 1. создаём песню (резервирует кредиты под job)
    resp = await client.post(
        "/v1/songs",
        json={"prompt": "an upbeat indie pop song", "genre": "pop", "mood": "happy"},
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["jobId"]

    # 2. резерв отражён в журнале кредитов (debit_reserve до прихода ошибки)
    ledger_before = (await client.get("/v1/billing/ledger", headers=headers)).json()
    assert any(e["kind"] == "debit_reserve" for e in ledger_before), ledger_before

    # 3. первый ERROR-конверт → пайплайн уходит в music-fallback, job остаётся running
    rid1 = await _provider_request_id(app, job_id)
    wh1 = await _emit_fal_error(client, rid1, error="primary model failed: OOM")
    assert wh1.status_code == 200, wh1.text
    assert wh1.json()["status"] == "ok"
    mid = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
    assert mid["status"] == "running", mid  # fallback запущен, ещё не терминал

    # 4. второй ERROR-конверт по новому request_id fallback-стадии → терминальный failed
    rid2 = await _provider_request_id(app, job_id)
    assert rid2 != rid1  # fallback использует новый provider_request_id
    wh2 = await _emit_fal_error(client, rid2, error="fallback model failed: OOM")
    assert wh2.status_code == 200, wh2.text
    assert wh2.json()["status"] == "ok"

    # 5. job переведён в failed с корректными error-полями (без трека)
    final = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
    assert final["status"] == "failed", final
    assert final["errorCode"] == "PROVIDER_FAILED"
    assert final["errorMessage"] == "fallback model failed: OOM"
    assert final["trackId"] is None

    # 6. зарезервированные кредиты возвращены (credit_release в журнале)
    ledger_after = (await client.get("/v1/billing/ledger", headers=headers)).json()
    assert any(e["kind"] == "credit_release" for e in ledger_after), ledger_after


@pytest.mark.asyncio
async def test_song_idempotency_key(client):
    headers = {**(await _auth(client)), "Idempotency-Key": "abc-123"}
    r1 = await client.post("/v1/songs", json={"prompt": "test"}, headers=headers)
    r2 = await client.post("/v1/songs", json={"prompt": "test"}, headers=headers)
    assert r1.json()["jobId"] == r2.json()["jobId"]
    assert r2.json()["deduplicated"] is True


@pytest.mark.asyncio
async def test_webhook_duplicate_ignored(client, app):
    headers = await _auth(client)
    job_id = (await client.post("/v1/songs", json={"prompt": "x"}, headers=headers)).json()["jobId"]
    rid = await _provider_request_id(app, job_id)
    first = await _emit_fal_completed(client, rid, media_url="https://cdn.local/a.mp3", duration=10)
    assert first.json()["status"] == "ok"
    second = await _emit_fal_completed(
        client, rid, media_url="https://cdn.local/a.mp3", duration=10
    )
    assert second.json()["status"] == "duplicate"


@pytest.mark.asyncio
async def test_get_other_users_job_forbidden(client):
    headers_a = await _auth(client)
    resp_a = await client.post("/v1/songs", json={"prompt": "x"}, headers=headers_a)
    job_id = resp_a.json()["jobId"]
    headers_b = await _auth(client)
    resp = await client.get(f"/v1/jobs/{job_id}", headers=headers_b)
    assert resp.status_code == 404
