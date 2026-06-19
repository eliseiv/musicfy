from __future__ import annotations

import pytest

from tests.helpers import (
    auth_headers,
    emit_fal_completed,
    grant_weekly_subscription,
    provider_request_id,
)


@pytest.mark.asyncio
async def test_video_happy_path_e2e(client, app):
    headers = await auth_headers(client)
    await grant_weekly_subscription(client, headers)  # weekly включает video-лимит

    # регистрируем push-токен (проверяем, что не падает на уведомлении)
    await client.post("/v1/devices/push-token", json={"token": "apns-token-xyz"}, headers=headers)

    resp = await client.post(
        "/v1/videos",
        json={
            "audioUrl": "https://cdn.local/song.mp3",
            "sourceVideoUrl": "https://cdn.local/avatar.mp4",
        },
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["jobId"]

    status = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
    assert status["currentStage"] == "lipsync"

    rid = await provider_request_id(app, job_id)
    wh = await emit_fal_completed(
        client, rid, media_url="https://cdn.local/result.mp4", duration=42.0
    )
    assert wh.json()["status"] == "ok"

    video = (await client.get(f"/v1/videos/{job_id}", headers=headers)).json()
    assert video["status"] == "completed"
    assert video["videoUrl"] == "https://cdn.local/result.mp4"


@pytest.mark.asyncio
async def test_video_requires_credits(client):
    headers = await auth_headers(client)
    resp = await client.post(
        "/v1/videos",
        json={"audioUrl": "https://cdn.local/a.mp3", "sourceVideoUrl": "https://cdn.local/v.mp4"},
        headers=headers,
    )
    assert resp.status_code == 402
