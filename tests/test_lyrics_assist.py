from __future__ import annotations

from uuid import UUID

import pytest

from app.domain.models.job import Job
from tests.helpers import auth_headers, grant_weekly_subscription


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "target", "text"),
    [
        ("inspire", "prompt", None),
        ("write", "lyrics", "a city at night"),
        ("enhance", "lyrics", "[Verse]\nCity lights"),
        ("enhance", "prompt", "sad piano"),
    ],
)
async def test_lyrics_assist_actions(client, action, target, text):
    headers = await auth_headers(client)
    payload = {
        "action": action,
        "target": target,
        "language": "en",
        "genre": "pop",
        "mood": "dreamy",
    }
    if text is not None:
        payload["text"] = text

    response = await client.post("/v1/lyrics/assist", json=payload, headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["content"]
    assert body["suggestedTitle"] == "Stub Song Title"
    if target == "lyrics":
        assert "[Verse]" in body["content"]


@pytest.mark.asyncio
async def test_lyrics_assist_enhance_requires_text(client):
    headers = await auth_headers(client)

    response = await client.post(
        "/v1/lyrics/assist",
        json={"action": "enhance", "target": "lyrics"},
        headers=headers,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_song_without_explicit_title_gets_llm_title(client, app):
    headers = await auth_headers(client)
    # Цена в clean_db существует, поэтому выдаём подписку штатным test-helper.
    await grant_weekly_subscription(client, headers)

    created = await client.post(
        "/v1/songs",
        json={"prompt": "an upbeat indie pop song", "language": "en"},
        headers=headers,
    )
    assert created.status_code == 202, created.text

    async with app.state.sessionmaker() as session:
        job = await session.get(Job, UUID(created.json()["jobId"]))
        assert job is not None
        assert job.input_payload["_generated_title"] == "Stub Song Title"


@pytest.mark.asyncio
async def test_explicit_song_title_skips_llm_title(client, app):
    headers = await auth_headers(client)
    await grant_weekly_subscription(client, headers)

    created = await client.post(
        "/v1/songs",
        json={"prompt": "indie pop", "title": "My Own Title"},
        headers=headers,
    )
    assert created.status_code == 202, created.text

    async with app.state.sessionmaker() as session:
        job = await session.get(Job, UUID(created.json()["jobId"]))
        assert job is not None
        assert "_generated_title" not in job.input_payload
