from __future__ import annotations

import pytest

from tests.helpers import auth_headers, grant_weekly_subscription


@pytest.mark.asyncio
async def test_moderation_blocks_prohibited_prompt(client):
    headers = await auth_headers(client)
    await grant_weekly_subscription(client, headers)
    resp = await client.post(
        "/v1/songs", json={"prompt": "a song with csam content"}, headers=headers
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "MODERATION_BLOCKED"


@pytest.mark.asyncio
async def test_analytics_event_and_legal_notices(client):
    headers = await auth_headers(client)
    ev = await client.post(
        "/v1/analytics/events",
        json={"name": "paywall_viewed", "properties": {"screen": "home"}},
        headers=headers,
    )
    assert ev.status_code == 204

    notices = await client.get("/v1/legal/notices")
    assert notices.status_code == 200
    keys = {n["key"] for n in notices.json()}
    assert "voice_rights" in keys and "copyright" in keys


@pytest.mark.asyncio
async def test_library_aggregates_tracks(client, app):
    from tests.helpers import emit_fal_completed, provider_request_id

    headers = await auth_headers(client)
    await grant_weekly_subscription(client, headers)
    job_id = (
        await client.post("/v1/songs", json={"prompt": "happy song"}, headers=headers)
    ).json()["jobId"]
    rid = await provider_request_id(app, job_id)
    await emit_fal_completed(client, rid, media_url="https://cdn.local/s.mp3", duration=30)

    lib = (await client.get("/v1/library", headers=headers)).json()
    assert len(lib["tracks"]) == 1
    assert lib["tracks"][0]["type"] == "song"
