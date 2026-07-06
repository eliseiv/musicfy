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

    sample_url = "https://cdn.local/voice.wav"
    resp = await client.post(
        "/v1/voices",
        json={
            "sampleAssetUrl": sample_url,
            "consentId": consent_id,
            "name": "My Voice",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "ready"
    assert body["providerVoiceId"]
    # VoiceProfileResponse: previewUrl == sample_asset_url, sampleDurationSeconds присутствует.
    assert "previewUrl" in body
    assert body["previewUrl"] == sample_url
    assert "sampleDurationSeconds" in body
    # sample_duration_seconds заполняется best-effort (probe недоступного URL → null допустим).
    assert body["sampleDurationSeconds"] is None or isinstance(body["sampleDurationSeconds"], int)

    library = (await client.get("/v1/voices", headers=headers)).json()
    assert len(library) == 1
    item = library[0]
    assert item["name"] == "My Voice"
    # Те же поля в list-представлении.
    assert item["previewUrl"] == sample_url
    assert "sampleDurationSeconds" in item
    assert item["sampleDurationSeconds"] is None or isinstance(item["sampleDurationSeconds"], int)


@pytest.mark.asyncio
async def test_consent_must_be_accepted(client):
    headers = await auth_headers(client)
    resp = await client.post(
        "/v1/voices/consent",
        json={"kind": "own_voice", "accepted": False},
        headers=headers,
    )
    assert resp.status_code == 400
