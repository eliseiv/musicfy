from __future__ import annotations

import pytest

from tests.helpers import (
    auth_headers,
    emit_fal_completed,
    grant_weekly_subscription,
    provider_request_id,
)


@pytest.mark.asyncio
async def test_cover_happy_path_e2e(client, app):
    headers = await auth_headers(client)
    await grant_weekly_subscription(client, headers)  # weekly включает cover-лимит

    # 1. создаём cover
    resp = await client.post(
        "/v1/covers",
        json={"source_audio_url": "https://cdn.local/input.mp3", "targetVoice": "english_male"},
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

    # 5. cover готов (без ffmpeg — деградированный путь: преобразованный вокал)
    final = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
    assert final["status"] == "completed", final
    track = (await client.get(f"/v1/tracks/{final['trackId']}", headers=headers)).json()
    assert track["kind"] == "cover"
    assert track["variants"][0]["audioUrl"]


@pytest.mark.asyncio
async def test_cover_requires_credits(client):
    headers = await auth_headers(client)
    resp = await client.post(
        "/v1/covers", json={"source_audio_url": "https://cdn.local/in.mp3"}, headers=headers
    )
    assert resp.status_code == 402
    assert resp.json()["error"]["code"] == "INSUFFICIENT_CREDITS"
