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
    body = json.dumps(
        {
            "request_id": request_id,
            "status": "completed",
            "result": {"media_url": media_url, "duration_seconds": duration},
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
