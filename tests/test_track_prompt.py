"""Интеграционные тесты поля ``prompt`` в API треков (ADR-008, часть B).

song-трек: Track.meta['prompt'] = сырой prompt → TrackResponse.prompt и
LibraryItem.prompt отдают его; title непустой (не «Untitled»).
cover-трек: prompt = None.
"""
from __future__ import annotations

import pytest

from tests.helpers import (
    auth_headers,
    emit_fal_completed,
    emit_fal_demucs_completed,
    grant_weekly_subscription,
    provider_request_id,
)

RAW_PROMPT = "an upbeat indie pop song about summer"


async def _complete_song(client, app, headers) -> str:
    """Создаёт и доводит song-джобу до completed, возвращает trackId."""
    resp = await client.post(
        "/v1/songs",
        json={"prompt": RAW_PROMPT, "genre": "pop", "mood": "happy"},
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["jobId"]
    rid = await provider_request_id(app, job_id)
    wh = await emit_fal_completed(
        client, rid, media_url="https://cdn.local/song.mp3", duration=42.0
    )
    assert wh.json()["status"] == "ok"
    final = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
    assert final["status"] == "completed", final
    return final["trackId"]


async def _complete_cover(client, app, headers) -> str:
    resp = await client.post(
        "/v1/covers",
        json={"source_audio_url": "https://cdn.local/input.mp3"},
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["jobId"]
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
        client, rid2, media_url="https://cdn.local/converted_vocal.wav", duration=30.0
    )
    final = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
    assert final["status"] == "completed", final
    return final["trackId"]


@pytest.mark.asyncio
async def test_song_track_response_exposes_raw_prompt(client, app):
    """GET /v1/tracks/{id}: prompt == сырой prompt, title непустой (не 'Untitled')."""
    headers = await auth_headers(client)
    await grant_weekly_subscription(client, headers)
    track_id = await _complete_song(client, app, headers)

    track = (await client.get(f"/v1/tracks/{track_id}", headers=headers)).json()
    assert track["kind"] == "song"
    assert track["prompt"] == RAW_PROMPT
    assert track["title"]
    assert track["title"].lower() != "untitled"


@pytest.mark.asyncio
async def test_song_library_item_exposes_prompt(client, app):
    """GET /v1/library: LibraryItem.prompt заполнен для song-трека."""
    headers = await auth_headers(client)
    await grant_weekly_subscription(client, headers)
    track_id = await _complete_song(client, app, headers)

    lib = (await client.get("/v1/library", headers=headers)).json()
    item = next((t for t in lib["tracks"] if t["id"] == track_id), None)
    assert item is not None, lib
    assert item["prompt"] == RAW_PROMPT
    assert item["title"]


@pytest.mark.asyncio
async def test_cover_track_prompt_is_none(client, app):
    """cover-трек: prompt = None и в TrackResponse, и в LibraryItem."""
    headers = await auth_headers(client)
    await grant_weekly_subscription(client, headers)
    track_id = await _complete_cover(client, app, headers)

    track = (await client.get(f"/v1/tracks/{track_id}", headers=headers)).json()
    assert track["kind"] == "cover"
    assert track["prompt"] is None

    lib = (await client.get("/v1/library", headers=headers)).json()
    item = next((t for t in lib["tracks"] if t["id"] == track_id), None)
    assert item is not None, lib
    assert item["prompt"] is None
