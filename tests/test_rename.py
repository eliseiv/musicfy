"""Переименование пользовательских ресурсов (ADR-012): PATCH /v1/{tracks|voices|videos}.

Плюс закрытие пробелов ADR-012:
- `title` видео сохраняется при создании (`derive_video_title` в `_finalize`), явный
  title 41–255 симв. сохраняется целиком (trim, БЕЗ усечения) → create и rename дают
  идентичный результат; дефолт по режиму при отсутствии title; старые видео (meta без
  title) → title=null.
- `GET /v1/library`: video-элемент `id == job_id` (round-trip open/rename/delete);
  video-Asset без meta.job_id пропускается (не даёт 500); tracks/voices id не менялись.
- Инварианты: rename не трогает баланс монет / credit_ledger / jobs / deleted_at.

Все тесты — на StubFalProvider (реальный fal не вызывается); пайплайн продвигается
webhook'ами. БД реальная (postgres 5544, autouse clean_db).
"""

from __future__ import annotations

import uuid as _uuid

import pytest
from sqlalchemy import func, select

from app.domain.enums import AssetKind, VoiceProfileStatus
from app.domain.models.asset import Asset
from app.domain.models.job import Job
from app.domain.models.voice import VoiceProfile
from tests.helpers import (
    auth_headers,
    emit_fal_completed,
    grant_coins,
    provider_request_id,
)

CLONE_SAMPLE_URL = "https://cdn.local/voice.wav"
GRANT = 100


# --------------------------------------------------------------------------- #
# Хелперы создания готовых ресурсов
# --------------------------------------------------------------------------- #
async def _create_ready_voice(client, headers, *, name: str = "My Clone") -> dict:
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
    return resp.json()


async def _create_song_track(client, app, headers) -> tuple[str, str]:
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
    return job_id, final["trackId"]


async def _create_video(
    client,
    app,
    headers,
    *,
    mode: str = "avatar_performance",
    title: str | None = None,
    media_url: str = "https://cdn.local/result.mp4",
) -> str:
    """Создаёт завершённое видео нужного режима; возвращает job_id."""
    body: dict = {"audioUrl": "https://cdn.local/song.mp3", "mode": mode}
    if mode == "avatar_performance":
        body["sourceVideoUrl"] = "https://cdn.local/avatar.mp4"
        body["style"] = "cinematic"
    elif mode == "visual_clip":
        body["prompt"] = "neon city flythrough"
        body["style"] = "anime"
    elif mode == "lyrics_video":
        body["lyrics"] = "la la la"
    if title is not None:
        body["title"] = title
    resp = await client.post("/v1/videos", json=body, headers=headers)
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["jobId"]
    rid = await provider_request_id(app, job_id)
    wh = await emit_fal_completed(client, rid, media_url=media_url, duration=42.0)
    assert wh.json()["status"] == "ok"
    video = (await client.get(f"/v1/videos/{job_id}", headers=headers)).json()
    assert video["status"] == "completed", video
    return job_id


async def _balance(client, headers) -> dict:
    return (await client.get("/v1/billing/balance", headers=headers)).json()


async def _ledger(client, headers) -> list:
    return (await client.get("/v1/billing/ledger", headers=headers)).json()


async def _count_jobs(app, headers, client) -> int:
    me = (await client.get("/v1/auth/me", headers=headers)).json()
    user_id = _uuid.UUID(me["userId"])
    async with app.state.sessionmaker() as session:
        stmt = select(func.count()).select_from(Job).where(Job.user_id == user_id)
        return int((await session.execute(stmt)).scalar_one())


# ========================================================================== #
# 1. Rename трека
# ========================================================================== #
@pytest.mark.asyncio
async def test_rename_track_200_updates_title_everywhere(client, app):
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT)
    _job_id, track_id = await _create_song_track(client, app, headers)

    resp = await client.patch(
        f"/v1/tracks/{track_id}", json={"title": "Новое имя"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "Новое имя"
    assert resp.json()["id"] == track_id

    # GET /tracks/{id}
    got = (await client.get(f"/v1/tracks/{track_id}", headers=headers)).json()
    assert got["title"] == "Новое имя"

    # library
    lib = (await client.get("/v1/library", headers=headers)).json()
    item = next(t for t in lib["tracks"] if t["id"] == track_id)
    assert item["title"] == "Новое имя"


@pytest.mark.asyncio
async def test_rename_track_trims_value(client, app):
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT)
    _job_id, track_id = await _create_song_track(client, app, headers)

    resp = await client.patch(
        f"/v1/tracks/{track_id}", json={"title": "  Trimmed  "}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "Trimmed"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "   ", "\t\n"])
async def test_rename_track_empty_or_whitespace_400(client, app, bad):
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT)
    _job_id, track_id = await _create_song_track(client, app, headers)

    resp = await client.patch(f"/v1/tracks/{track_id}", json={"title": bad}, headers=headers)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_rename_track_too_long_400(client, app):
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT)
    _job_id, track_id = await _create_song_track(client, app, headers)

    resp = await client.patch(
        f"/v1/tracks/{track_id}", json={"title": "x" * 256}, headers=headers
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "INVALID_INPUT"

    # ровно 255 допустимо
    ok = await client.patch(
        f"/v1/tracks/{track_id}", json={"title": "y" * 255}, headers=headers
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["title"] == "y" * 255


@pytest.mark.asyncio
async def test_rename_track_foreign_and_unknown_404(client, app):
    headers_a = await auth_headers(client)
    await grant_coins(client, headers_a, coins=GRANT)
    _job_id, track_id = await _create_song_track(client, app, headers_a)

    headers_b = await auth_headers(client)
    foreign = await client.patch(
        f"/v1/tracks/{track_id}", json={"title": "hack"}, headers=headers_b
    )
    assert foreign.status_code == 404, foreign.text
    assert foreign.json()["error"]["code"] == "TRACK_NOT_FOUND"

    unknown = await client.patch(
        f"/v1/tracks/{_uuid.uuid4()}", json={"title": "x"}, headers=headers_a
    )
    assert unknown.status_code == 404, unknown.text
    assert unknown.json()["error"]["code"] == "TRACK_NOT_FOUND"

    # у владельца title не изменился чужим запросом
    got = (await client.get(f"/v1/tracks/{track_id}", headers=headers_a)).json()
    assert got["title"] != "hack"


@pytest.mark.asyncio
async def test_rename_track_soft_deleted_404(client, app):
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT)
    _job_id, track_id = await _create_song_track(client, app, headers)

    assert (await client.delete(f"/v1/tracks/{track_id}", headers=headers)).status_code == 204
    resp = await client.patch(
        f"/v1/tracks/{track_id}", json={"title": "ghost"}, headers=headers
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "TRACK_NOT_FOUND"


@pytest.mark.asyncio
async def test_rename_track_idempotent(client, app):
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT)
    _job_id, track_id = await _create_song_track(client, app, headers)

    first = await client.patch(
        f"/v1/tracks/{track_id}", json={"title": "Same"}, headers=headers
    )
    assert first.status_code == 200, first.text
    repeat = await client.patch(
        f"/v1/tracks/{track_id}", json={"title": "Same"}, headers=headers
    )
    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["title"] == "Same"


# ========================================================================== #
# 2. Rename голоса
# ========================================================================== #
@pytest.mark.asyncio
async def test_rename_voice_200_updates_name(client):
    headers = await auth_headers(client)
    voice_id = (await _create_ready_voice(client, headers))["id"]

    resp = await client.patch(
        f"/v1/voices/{voice_id}", json={"name": "Мой голос"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Мой голос"
    assert resp.json()["id"] == voice_id
    # job_id в rename-ответе = null (ADR-012 §1)
    assert resp.json().get("jobId") is None

    listing = (await client.get("/v1/voices", headers=headers)).json()
    item = next(v for v in listing if v["id"] == voice_id)
    assert item["name"] == "Мой голос"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "   "])
async def test_rename_voice_empty_400(client, bad):
    headers = await auth_headers(client)
    voice_id = (await _create_ready_voice(client, headers))["id"]
    resp = await client.patch(f"/v1/voices/{voice_id}", json={"name": bad}, headers=headers)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_rename_voice_too_long_400(client):
    headers = await auth_headers(client)
    voice_id = (await _create_ready_voice(client, headers))["id"]

    resp = await client.patch(
        f"/v1/voices/{voice_id}", json={"name": "z" * 121}, headers=headers
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "INVALID_INPUT"

    ok = await client.patch(
        f"/v1/voices/{voice_id}", json={"name": "z" * 120}, headers=headers
    )
    assert ok.status_code == 200, ok.text


@pytest.mark.asyncio
async def test_rename_voice_foreign_and_unknown_404(client):
    headers_a = await auth_headers(client)
    voice_id = (await _create_ready_voice(client, headers_a))["id"]

    headers_b = await auth_headers(client)
    foreign = await client.patch(
        f"/v1/voices/{voice_id}", json={"name": "hack"}, headers=headers_b
    )
    assert foreign.status_code == 404, foreign.text
    assert foreign.json()["error"]["code"] == "VOICE_PROFILE_NOT_FOUND"

    unknown = await client.patch(
        f"/v1/voices/{_uuid.uuid4()}", json={"name": "x"}, headers=headers_a
    )
    assert unknown.status_code == 404, unknown.text
    assert unknown.json()["error"]["code"] == "VOICE_PROFILE_NOT_FOUND"


@pytest.mark.asyncio
async def test_rename_voice_soft_deleted_404(client):
    headers = await auth_headers(client)
    voice_id = (await _create_ready_voice(client, headers))["id"]

    assert (await client.delete(f"/v1/voices/{voice_id}", headers=headers)).status_code == 204
    resp = await client.patch(
        f"/v1/voices/{voice_id}", json={"name": "ghost"}, headers=headers
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "VOICE_PROFILE_NOT_FOUND"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [VoiceProfileStatus.pending, VoiceProfileStatus.failed])
async def test_rename_voice_pending_or_failed_allowed(client, app, status):
    """Фактическое поведение: rename разрешён для любого НЕ-удалённого профиля
    (owner-check фильтрует только deleted_at). Переводим профиль в pending/failed
    напрямую в БД и подтверждаем 200."""
    headers = await auth_headers(client)
    voice_id = (await _create_ready_voice(client, headers))["id"]

    async with app.state.sessionmaker() as session:
        async with session.begin():
            profile = await session.get(VoiceProfile, _uuid.UUID(voice_id))
            profile.status = status

    resp = await client.patch(
        f"/v1/voices/{voice_id}", json={"name": "Renamed"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Renamed"
    assert resp.json()["status"] == status.value


# ========================================================================== #
# 3. Rename видео
# ========================================================================== #
@pytest.mark.asyncio
async def test_rename_video_200_updates_title(client, app):
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT)
    job_id = await _create_video(client, app, headers)

    resp = await client.patch(
        f"/v1/videos/{job_id}", json={"title": "Клип"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "Клип"
    assert resp.json()["jobId"] == job_id

    got = (await client.get(f"/v1/videos/{job_id}", headers=headers)).json()
    assert got["title"] == "Клип"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "   "])
async def test_rename_video_empty_400(client, app, bad):
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT)
    job_id = await _create_video(client, app, headers)
    resp = await client.patch(f"/v1/videos/{job_id}", json={"title": bad}, headers=headers)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_rename_video_foreign_and_unknown_404(client, app):
    headers_a = await auth_headers(client)
    await grant_coins(client, headers_a, coins=GRANT)
    job_id = await _create_video(client, app, headers_a)

    headers_b = await auth_headers(client)
    foreign = await client.patch(
        f"/v1/videos/{job_id}", json={"title": "hack"}, headers=headers_b
    )
    assert foreign.status_code == 404, foreign.text
    assert foreign.json()["error"]["code"] == "VIDEO_NOT_FOUND"

    unknown = await client.patch(
        f"/v1/videos/{_uuid.uuid4()}", json={"title": "x"}, headers=headers_a
    )
    assert unknown.status_code == 404, unknown.text
    assert unknown.json()["error"]["code"] == "VIDEO_NOT_FOUND"


@pytest.mark.asyncio
async def test_rename_video_soft_deleted_404(client, app):
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT)
    job_id = await _create_video(client, app, headers)

    assert (await client.delete(f"/v1/videos/{job_id}", headers=headers)).status_code == 204
    resp = await client.patch(
        f"/v1/videos/{job_id}", json={"title": "ghost"}, headers=headers
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "VIDEO_NOT_FOUND"


@pytest.mark.asyncio
async def test_rename_video_not_ready_404(client, app):
    """Видео ещё не готово (нет video-Asset) → 404 (нечего переименовывать, ADR-012 §1)."""
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT)
    resp = await client.post(
        "/v1/videos",
        json={
            "mode": "avatar_performance",
            "audioUrl": "https://cdn.local/song.mp3",
            "sourceVideoUrl": "https://cdn.local/avatar.mp4",
        },
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["jobId"]  # webhook НЕ эмитим → ассета нет

    rename = await client.patch(
        f"/v1/videos/{job_id}", json={"title": "early"}, headers=headers
    )
    assert rename.status_code == 404, rename.text
    assert rename.json()["error"]["code"] == "VIDEO_NOT_FOUND"


# ========================================================================== #
# 4. title видео сохраняется при создании (без усечения) == rename
# ========================================================================== #
@pytest.mark.asyncio
async def test_video_explicit_title_41_255_preserved_no_truncation(client, app):
    """Явный title длиной 41–255 симв. сохраняется целиком при create (trim, БЕЗ
    усечения); rename тем же значением даёт идентичный результат (create==rename)."""
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT)
    long_title = "A very long descriptive music video title that clearly exceeds forty chars"
    assert 41 <= len(long_title) <= 255

    job_id = await _create_video(client, app, headers, mode="visual_clip", title=long_title)
    got = (await client.get(f"/v1/videos/{job_id}", headers=headers)).json()
    assert got["title"] == long_title  # ровно тот же, без усечения до 40

    # rename тем же значением → идентично (create==rename)
    renamed = await client.patch(
        f"/v1/videos/{job_id}", json={"title": long_title}, headers=headers
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["title"] == long_title


@pytest.mark.asyncio
async def test_video_explicit_title_trimmed_on_create(client, app):
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT)
    job_id = await _create_video(client, app, headers, mode="visual_clip", title="  Spaced  ")
    got = (await client.get(f"/v1/videos/{job_id}", headers=headers)).json()
    assert got["title"] == "Spaced"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,expected",
    [
        ("avatar_performance", "Avatar Video"),
        ("visual_clip", "Visual Clip"),
    ],
)
async def test_video_default_title_by_mode_e2e(client, app, mode, expected):
    """E2E-дефолт для режимов, достижимых до completed (avatar/visual). lyrics_video
    без ffmpeg до completed не доходит (release, см. test_video_modes) — его дефолт
    покрыт unit-тестом derive_video_title ниже."""
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT)
    job_id = await _create_video(client, app, headers, mode=mode)  # без title
    got = (await client.get(f"/v1/videos/{job_id}", headers=headers)).json()
    assert got["title"] == expected


# --- unit-тесты derive_video_title (чистая функция; покрывает lyrics_video/fallback) ---
@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"title": "Custom", "mode": "visual_clip"}, "Custom"),  # explicit > mode
        ({"title": "  Trim me  "}, "Trim me"),  # trim
        ({"title": "x" * 200, "mode": "visual_clip"}, "x" * 200),  # без усечения (>40)
        ({"mode": "avatar_performance"}, "Avatar Video"),
        ({"mode": "visual_clip"}, "Visual Clip"),
        ({"mode": "lyrics_video"}, "Lyrics Video"),
        ({"mode": "unknown_mode"}, "Music Video"),  # fallback
        ({}, "Music Video"),
        (None, "Music Video"),
        ({"title": "   ", "mode": "visual_clip"}, "Visual Clip"),  # пустой title → дефолт
    ],
)
def test_derive_video_title_unit(payload, expected):
    from app.domain.services.video_title import derive_video_title

    assert derive_video_title(payload) == expected


@pytest.mark.asyncio
async def test_old_video_meta_without_title_is_null(client, app):
    """Обратная совместимость: у видео-Asset без ключа meta.title → title=null."""
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT)
    job_id = await _create_video(client, app, headers)

    # эмулируем «старый» ассет: убираем ключ title из meta (реассайн словаря)
    async with app.state.sessionmaker() as session:
        async with session.begin():
            stmt = (
                select(Asset)
                .where(Asset.kind == AssetKind.video)
                .where(Asset.meta["job_id"].astext == str(job_id))
                .limit(1)
            )
            asset = (await session.execute(stmt)).scalars().first()
            asset.meta = {k: v for k, v in (asset.meta or {}).items() if k != "title"}

    got = (await client.get(f"/v1/videos/{job_id}", headers=headers)).json()
    assert got["title"] is None, got


# ========================================================================== #
# 5. library: video id == job_id (round-trip), tracks/voices id неизменны
# ========================================================================== #
@pytest.mark.asyncio
async def test_library_video_id_is_job_id_and_roundtrip(client, app):
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT)
    job_id = await _create_video(client, app, headers, title="Round Trip")

    lib = (await client.get("/v1/library", headers=headers)).json()
    videos = [v for v in lib["videos"] if v["type"] == "video"]
    assert len(videos) == 1, lib
    item = videos[0]
    assert item["id"] == job_id, item
    assert item["title"] == "Round Trip"

    # round-trip по library.id: GET/PATCH/DELETE принимают этот id
    assert (await client.get(f"/v1/videos/{item['id']}", headers=headers)).status_code == 200
    patched = await client.patch(
        f"/v1/videos/{item['id']}", json={"title": "Via Library"}, headers=headers
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["title"] == "Via Library"
    assert (await client.delete(f"/v1/videos/{item['id']}", headers=headers)).status_code == 204


@pytest.mark.asyncio
async def test_library_soft_deleted_video_absent(client, app):
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT)
    job_id = await _create_video(client, app, headers)

    assert (await client.delete(f"/v1/videos/{job_id}", headers=headers)).status_code == 204
    lib = (await client.get("/v1/library", headers=headers)).json()
    assert all(v["id"] != job_id for v in lib["videos"]), lib


@pytest.mark.asyncio
async def test_library_track_and_voice_id_unchanged(client, app):
    """tracks[].id == Track.id (принимается /tracks/{id}); voices[].id == VoiceProfile.id."""
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT)
    _job_id, track_id = await _create_song_track(client, app, headers)
    voice_id = (await _create_ready_voice(client, headers))["id"]

    lib = (await client.get("/v1/library", headers=headers)).json()
    track_item = next(t for t in lib["tracks"] if t["id"] == track_id)
    voice_item = next(v for v in lib["voices"] if v["id"] == voice_id)
    # id из library принимается соответствующими эндпоинтами
    assert (await client.get(f"/v1/tracks/{track_item['id']}", headers=headers)).status_code == 200
    assert (await client.get("/v1/voices", headers=headers)).status_code == 200
    assert voice_item["id"] == voice_id


@pytest.mark.asyncio
async def test_library_video_asset_without_job_id_skipped(client, app):
    """video-Asset без meta.job_id пропускается: library → 200 и элемент отсутствует."""
    headers = await auth_headers(client)
    me = (await client.get("/v1/auth/me", headers=headers)).json()
    user_id = _uuid.UUID(me["userId"])
    marker_url = "https://cdn.local/orphan-no-jobid.mp4"

    async with app.state.sessionmaker() as session:
        async with session.begin():
            session.add(
                Asset(
                    user_id=user_id,
                    kind=AssetKind.video,
                    url=marker_url,
                    meta={"mode": "visual_clip"},  # без job_id
                )
            )

    resp = await client.get("/v1/library", headers=headers)
    assert resp.status_code == 200, resp.text
    assert all(v["url"] != marker_url for v in resp.json()["videos"]), resp.json()


# ========================================================================== #
# 6. Инварианты: rename не трогает баланс / ledger / jobs / deleted_at
# ========================================================================== #
@pytest.mark.asyncio
async def test_rename_track_invariants(client, app):
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT)
    _job_id, track_id = await _create_song_track(client, app, headers)

    bal_before = await _balance(client, headers)
    ledger_before = await _ledger(client, headers)
    jobs_before = await _count_jobs(app, headers, client)

    resp = await client.patch(
        f"/v1/tracks/{track_id}", json={"title": "Renamed"}, headers=headers
    )
    assert resp.status_code == 200, resp.text

    assert await _balance(client, headers) == bal_before
    assert len(await _ledger(client, headers)) == len(ledger_before)
    assert await _count_jobs(app, headers, client) == jobs_before
    # deleted_at не тронут — ресурс по-прежнему доступен
    assert (await client.get(f"/v1/tracks/{track_id}", headers=headers)).status_code == 200


@pytest.mark.asyncio
async def test_rename_video_invariants(client, app):
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT)
    job_id = await _create_video(client, app, headers)

    bal_before = await _balance(client, headers)
    ledger_before = await _ledger(client, headers)
    jobs_before = await _count_jobs(app, headers, client)

    resp = await client.patch(
        f"/v1/videos/{job_id}", json={"title": "Renamed"}, headers=headers
    )
    assert resp.status_code == 200, resp.text

    assert await _balance(client, headers) == bal_before
    assert len(await _ledger(client, headers)) == len(ledger_before)
    assert await _count_jobs(app, headers, client) == jobs_before
    # видео по-прежнему доступно (deleted_at не тронут)
    assert (await client.get(f"/v1/videos/{job_id}", headers=headers)).json()["videoUrl"]
