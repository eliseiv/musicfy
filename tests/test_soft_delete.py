"""Soft-delete пользовательских ресурсов (ADR-011): DELETE /v1/voices|tracks|videos.

Все тесты — на StubFalProvider (реальный fal не вызывается); пайплайн продвигается
webhook'ами. Покрывает:
- 204 успех / 404 (повтор/чужой/несуществующий) для voices/tracks/videos;
- скрытие из GET /voices, GET /tracks/{id}, GET /library, GET /videos/{job_id};
- resolve-блок удалённого источника: cover с удалённым голосом → 422 unknown_voice;
  видео из удалённого трека → 404 TRACK_NOT_FOUND;
- GET /jobs/{job_id} удалённого трека → track_id=null (закрытие протечки идентификатора);
- инварианты: удаление НЕ меняет баланс монет; credit_ledger/Job сохраняются;
- идемпотентность финализатора: повторный webhook после soft-delete не создаёт дубль трека.
"""

from __future__ import annotations

import uuid as _uuid

import pytest
from sqlalchemy import func, select

from app.domain.models.track import Track
from tests.helpers import (
    auth_headers,
    emit_fal_completed,
    grant_coins,
    grant_weekly_subscription,
    provider_request_id,
)

CLONE_SAMPLE_URL = "https://cdn.local/voice.wav"
VIDEO_PRICE = 30
SONG_PRICE = 10


# --------------------------------------------------------------------------- #
# Фикстуры-хелперы
# --------------------------------------------------------------------------- #
async def _create_ready_voice(client, headers, *, name: str = "My Clone") -> dict:
    """Создаёт ready voice-профиль текущего пользователя, возвращает тело ответа."""
    consent = await client.post(
        "/v1/voices/consent",
        json={"kind": "own_voice", "accepted": True, "statement": "my voice"},
        headers=headers,
    )
    consent_id = consent.json()["id"]
    resp = await client.post(
        "/v1/voices",
        json={"sampleAssetUrl": CLONE_SAMPLE_URL, "consentId": consent_id, "name": name},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "ready", body
    return body


async def _create_song_track(client, app, headers) -> tuple[str, str]:
    """Создаёт завершённый song-трек; возвращает (job_id, track_id)."""
    resp = await client.post(
        "/v1/songs",
        json={"prompt": "an upbeat indie pop song", "genre": "pop", "mood": "happy"},
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
    assert final["trackId"], final
    return job_id, final["trackId"]


async def _create_video(client, app, headers) -> str:
    """Создаёт завершённое видео (avatar_performance); возвращает job_id."""
    resp = await client.post(
        "/v1/videos",
        json={
            "mode": "avatar_performance",
            "audioUrl": "https://cdn.local/song.mp3",
            "sourceVideoUrl": "https://cdn.local/avatar.mp4",
            "style": "cinematic",
        },
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["jobId"]
    rid = await provider_request_id(app, job_id)
    wh = await emit_fal_completed(
        client, rid, media_url="https://cdn.local/result.mp4", duration=42.0
    )
    assert wh.json()["status"] == "ok"
    video = (await client.get(f"/v1/videos/{job_id}", headers=headers)).json()
    assert video["status"] == "completed", video
    assert video["videoUrl"], video
    return job_id


async def _balance(client, headers) -> dict:
    return (await client.get("/v1/billing/balance", headers=headers)).json()


async def _ledger(client, headers) -> list:
    return (await client.get("/v1/billing/ledger", headers=headers)).json()


async def _count_tracks_for_job(app, job_id: str) -> int:
    """Считает ВСЕ строки tracks по job_id (включая soft-deleted) — детектор дублей."""
    async with app.state.sessionmaker() as session:
        stmt = select(func.count()).select_from(Track).where(
            Track.job_id == _uuid.UUID(job_id)
        )
        return int((await session.execute(stmt)).scalar_one())


# --------------------------------------------------------------------------- #
# 1. Voices
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_delete_voice_204_and_hidden_from_listing(client):
    headers = await auth_headers(client)
    profile = await _create_ready_voice(client, headers)
    voice_id = profile["id"]

    # присутствует в листинге до удаления
    listing = (await client.get("/v1/voices", headers=headers)).json()
    assert any(v["id"] == voice_id for v in listing), listing

    resp = await client.delete(f"/v1/voices/{voice_id}", headers=headers)
    assert resp.status_code == 204, resp.text
    assert resp.content == b""

    # исчез из GET /voices
    listing = (await client.get("/v1/voices", headers=headers)).json()
    assert all(v["id"] != voice_id for v in listing), listing

    # исчез из GET /library.voices
    library = (await client.get("/v1/library", headers=headers)).json()
    assert all(v["id"] != voice_id for v in library["voices"]), library


@pytest.mark.asyncio
async def test_delete_voice_repeat_is_404_idempotent(client):
    headers = await auth_headers(client)
    voice_id = (await _create_ready_voice(client, headers))["id"]
    first = await client.delete(f"/v1/voices/{voice_id}", headers=headers)
    assert first.status_code == 204, first.text
    repeat = await client.delete(f"/v1/voices/{voice_id}", headers=headers)
    assert repeat.status_code == 404, repeat.text
    assert repeat.json()["error"]["code"] == "VOICE_PROFILE_NOT_FOUND"


@pytest.mark.asyncio
async def test_delete_voice_foreign_is_404(client):
    headers_a = await auth_headers(client)
    voice_id = (await _create_ready_voice(client, headers_a))["id"]

    headers_b = await auth_headers(client)
    resp = await client.delete(f"/v1/voices/{voice_id}", headers=headers_b)
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "VOICE_PROFILE_NOT_FOUND"
    # у владельца голос по-прежнему жив
    listing = (await client.get("/v1/voices", headers=headers_a)).json()
    assert any(v["id"] == voice_id for v in listing), listing


@pytest.mark.asyncio
async def test_delete_voice_unknown_id_is_404(client):
    headers = await auth_headers(client)
    resp = await client.delete(f"/v1/voices/{_uuid.uuid4()}", headers=headers)
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_cover_with_deleted_voice_uuid_is_422_unknown_voice(client):
    """Резолв-блок: cover c UUID удалённого клона → 422 unknown_voice (симметрия трекам)."""
    headers = await auth_headers(client)
    await grant_weekly_subscription(client, headers)
    profile = await _create_ready_voice(client, headers)
    voice_id = profile["id"]

    # до удаления UUID резолвится (202)
    ok = await client.post(
        "/v1/covers",
        json={"source_audio_url": "https://cdn.local/input.mp3", "targetVoice": voice_id},
        headers=headers,
    )
    assert ok.status_code == 202, ok.text

    del_resp = await client.delete(f"/v1/voices/{voice_id}", headers=headers)
    assert del_resp.status_code == 204, del_resp.text

    # после soft-delete тот же UUID трактуется как несуществующий → 422 unknown_voice
    resp = await client.post(
        "/v1/covers",
        json={"source_audio_url": "https://cdn.local/input.mp3", "targetVoice": voice_id},
        headers=headers,
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["details"]["reason"] == "unknown_voice"


# --------------------------------------------------------------------------- #
# 2. Tracks
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_delete_track_204_and_hidden(client, app):
    headers = await auth_headers(client)
    await grant_weekly_subscription(client, headers)
    _job_id, track_id = await _create_song_track(client, app, headers)

    # присутствует в library до удаления
    library = (await client.get("/v1/library", headers=headers)).json()
    assert any(t["id"] == track_id for t in library["tracks"]), library

    resp = await client.delete(f"/v1/tracks/{track_id}", headers=headers)
    assert resp.status_code == 204, resp.text
    assert resp.content == b""

    # GET /tracks/{id} → 404
    got = await client.get(f"/v1/tracks/{track_id}", headers=headers)
    assert got.status_code == 404, got.text
    assert got.json()["error"]["code"] == "TRACK_NOT_FOUND"

    # исчез из GET /library.tracks
    library = (await client.get("/v1/library", headers=headers)).json()
    assert all(t["id"] != track_id for t in library["tracks"]), library


@pytest.mark.asyncio
async def test_delete_track_repeat_and_foreign_404(client, app):
    headers = await auth_headers(client)
    await grant_weekly_subscription(client, headers)
    _job_id, track_id = await _create_song_track(client, app, headers)

    first = await client.delete(f"/v1/tracks/{track_id}", headers=headers)
    assert first.status_code == 204, first.text
    repeat = await client.delete(f"/v1/tracks/{track_id}", headers=headers)
    assert repeat.status_code == 404, repeat.text
    assert repeat.json()["error"]["code"] == "TRACK_NOT_FOUND"

    # чужой трек: пользователь B создаёт свой, A не может его удалить
    headers_b = await auth_headers(client)
    await grant_weekly_subscription(client, headers_b)
    _jb, track_b = await _create_song_track(client, app, headers_b)
    foreign = await client.delete(f"/v1/tracks/{track_b}", headers=headers)
    assert foreign.status_code == 404, foreign.text
    # у владельца трек жив
    assert (await client.get(f"/v1/tracks/{track_b}", headers=headers_b)).status_code == 200


@pytest.mark.asyncio
async def test_jobs_track_id_null_after_track_delete(client, app):
    """Протечка закрыта: GET /jobs/{job_id} после soft-delete трека → track_id=null."""
    headers = await auth_headers(client)
    await grant_weekly_subscription(client, headers)
    job_id, track_id = await _create_song_track(client, app, headers)

    before = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
    assert before["trackId"] == track_id, before

    del_resp = await client.delete(f"/v1/tracks/{track_id}", headers=headers)
    assert del_resp.status_code == 204, del_resp.text

    after = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
    assert after["trackId"] is None, after


@pytest.mark.asyncio
async def test_video_from_deleted_track_is_404_track_not_found(client, app):
    """Резолв-блок: создание видео из удалённого трека (trackId) → 404 TRACK_NOT_FOUND."""
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=100)
    _job_id, track_id = await _create_song_track(client, app, headers)

    del_resp = await client.delete(f"/v1/tracks/{track_id}", headers=headers)
    assert del_resp.status_code == 204, del_resp.text

    resp = await client.post(
        "/v1/videos",
        json={"mode": "visual_clip", "trackId": track_id, "prompt": "neon city flythrough"},
        headers=headers,
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "TRACK_NOT_FOUND"


# --------------------------------------------------------------------------- #
# 3. Videos
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_delete_video_204_and_hidden(client, app):
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=100)
    job_id = await _create_video(client, app, headers)

    # присутствует в library до удаления
    library = (await client.get("/v1/library", headers=headers)).json()
    assert any(v["url"] == "https://cdn.local/result.mp4" for v in library["videos"]), library

    resp = await client.delete(f"/v1/videos/{job_id}", headers=headers)
    assert resp.status_code == 204, resp.text
    assert resp.content == b""

    # GET /videos/{job_id}: джоба существует → 200 с videoUrl=null (video-asset отфильтрован)
    got = await client.get(f"/v1/videos/{job_id}", headers=headers)
    assert got.status_code == 200, got.text
    assert got.json()["videoUrl"] is None, got.json()

    # исчез из GET /library.videos
    library = (await client.get("/v1/library", headers=headers)).json()
    assert all(v["url"] != "https://cdn.local/result.mp4" for v in library["videos"]), library


@pytest.mark.asyncio
async def test_delete_video_repeat_and_foreign_404(client, app):
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=100)
    job_id = await _create_video(client, app, headers)

    first = await client.delete(f"/v1/videos/{job_id}", headers=headers)
    assert first.status_code == 204, first.text
    repeat = await client.delete(f"/v1/videos/{job_id}", headers=headers)
    assert repeat.status_code == 404, repeat.text
    assert repeat.json()["error"]["code"] == "VIDEO_NOT_FOUND"

    # чужой: B создаёт видео, A не может удалить
    headers_b = await auth_headers(client)
    await grant_coins(client, headers_b, coins=100)
    job_b = await _create_video(client, app, headers_b)
    foreign = await client.delete(f"/v1/videos/{job_b}", headers=headers)
    assert foreign.status_code == 404, foreign.text
    assert foreign.json()["error"]["code"] == "VIDEO_NOT_FOUND"
    # у владельца видео живо
    assert (await client.get(f"/v1/videos/{job_b}", headers=headers_b)).json()["videoUrl"]


@pytest.mark.asyncio
async def test_delete_video_unknown_job_is_404(client):
    headers = await auth_headers(client)
    resp = await client.delete(f"/v1/videos/{_uuid.uuid4()}", headers=headers)
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "VIDEO_NOT_FOUND"


# --------------------------------------------------------------------------- #
# 4. Инварианты: монеты не возвращаются, ledger/Job сохраняются
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_delete_track_does_not_refund_coins_or_touch_ledger(client, app):
    headers = await auth_headers(client)
    await grant_weekly_subscription(client, headers)
    job_id, track_id = await _create_song_track(client, app, headers)

    bal_before = await _balance(client, headers)
    ledger_before = await _ledger(client, headers)

    del_resp = await client.delete(f"/v1/tracks/{track_id}", headers=headers)
    assert del_resp.status_code == 204, del_resp.text

    bal_after = await _balance(client, headers)
    ledger_after = await _ledger(client, headers)

    # монеты не возвращены (баланс не изменился)
    assert bal_after == bal_before, (bal_before, bal_after)
    # credit_ledger неизменяем — число записей то же
    assert len(ledger_after) == len(ledger_before), (ledger_before, ledger_after)
    # Job сохранён (история доступна)
    assert (await client.get(f"/v1/jobs/{job_id}", headers=headers)).status_code == 200


@pytest.mark.asyncio
async def test_delete_video_does_not_refund_coins_or_touch_ledger(client, app):
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=100)
    job_id = await _create_video(client, app, headers)

    bal_before = await _balance(client, headers)
    ledger_before = await _ledger(client, headers)

    del_resp = await client.delete(f"/v1/videos/{job_id}", headers=headers)
    assert del_resp.status_code == 204, del_resp.text

    bal_after = await _balance(client, headers)
    ledger_after = await _ledger(client, headers)

    assert bal_after == bal_before, (bal_before, bal_after)
    assert len(ledger_after) == len(ledger_before), (ledger_before, ledger_after)
    # Job сохранён (история/биллинг)
    assert (await client.get(f"/v1/jobs/{job_id}", headers=headers)).status_code == 200


# --------------------------------------------------------------------------- #
# 5. Finalize-дедуп / идемпотентность после soft-delete
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_finalize_idempotent_after_track_soft_delete(client, app):
    """После soft-delete трека повторный (дублирующий) webhook финализатора НЕ создаёт
    второй трек по тому же job_id: get_by_job_id(include_deleted=True) видит удалённый
    трек и не плодит дубль. Проверяем счётчиком строк tracks по job_id (вкл. удалённые)."""
    headers = await auth_headers(client)
    await grant_weekly_subscription(client, headers)
    job_id, track_id = await _create_song_track(client, app, headers)

    assert await _count_tracks_for_job(app, job_id) == 1

    # soft-delete трека
    del_resp = await client.delete(f"/v1/tracks/{track_id}", headers=headers)
    assert del_resp.status_code == 204, del_resp.text

    # повторный webhook той же стадии (тот же request_id) → dedup, финализатор не плодит дубль
    rid = await provider_request_id(app, job_id)
    replay = await emit_fal_completed(
        client, rid, media_url="https://cdn.local/song.mp3", duration=42.0
    )
    assert replay.json()["status"] == "duplicate", replay.json()

    # число треков по job_id не выросло (по-прежнему единственный, теперь soft-deleted)
    assert await _count_tracks_for_job(app, job_id) == 1
