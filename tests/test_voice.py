from __future__ import annotations

import pytest

from tests.helpers import auth_headers


@pytest.mark.asyncio
async def test_voice_clone_rejects_foreign_consent(client):
    # Согласие создано пользователем A.
    headers_a = await auth_headers(client)
    consent = await client.post(
        "/v1/voices/consent",
        json={"kind": "own_voice", "accepted": True},
        headers=headers_a,
    )
    consent_id = consent.json()["id"]

    # Пользователь B пытается использовать чужое согласие → consent_check падает.
    headers_b = await auth_headers(client)
    resp = await client.post(
        "/v1/voices",
        json={"sampleAssetUrl": "https://cdn.local/voice.wav", "consentId": consent_id},
        headers=headers_b,
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "failed"


@pytest.mark.asyncio
async def test_voice_clone_happy_path(client):
    headers = await auth_headers(client)
    consent = await client.post(
        "/v1/voices/consent",
        json={"kind": "own_voice", "accepted": True, "statement": "It's my voice"},
        headers=headers,
    )
    assert consent.status_code == 200, consent.text
    consent_id = consent.json()["id"]

    resp = await client.post(
        "/v1/voices",
        json={
            "sampleAssetUrl": "https://cdn.local/voice.wav",
            "consentId": consent_id,
            "name": "My Voice",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "ready"
    assert body["providerVoiceId"]

    library = (await client.get("/v1/voices", headers=headers)).json()
    assert len(library) == 1
    assert library[0]["name"] == "My Voice"


@pytest.mark.asyncio
async def test_consent_must_be_accepted(client):
    headers = await auth_headers(client)
    resp = await client.post(
        "/v1/voices/consent",
        json={"kind": "own_voice", "accepted": False},
        headers=headers,
    )
    assert resp.status_code == 400
