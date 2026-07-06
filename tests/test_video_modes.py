"""Feature B — AI music video на 3 режима (ADR-007), на стабе fal.

Покрывает: маршрутизацию mode→fal-модель, стадии пайплайна, инвариант
provider_model == вызванной модели (desync-guard), валидатор режимов, резолв
«My track», protocol-conformance стаба и инварианты монет (capture/release).

Реальный fal не дёргается (FAL_USE_STUB=true). ffmpeg в тест-среде отсутствует —
поэтому visual_clip мукс деградирует (quality_flag), а lyrics_video рендер падает
и возвращает монеты (release), что явно проверяется.

Замечание по контракту статусов: ошибки схемы (pydantic, включая mode-валидатор)
в этом приложении отдаются как 400 INVALID_INPUT (RequestValidationError-хендлер),
а не 422. 422 отдаёт только endpoint-level ValidationFailed (unknown variant).
"""
from __future__ import annotations

import inspect
import uuid as _uuid

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.domain.enums import AssetKind
from app.domain.models.asset import Asset
from app.domain.models.job import Job
from app.domain.providers.fal.stub import StubFalProvider
from tests.helpers import (
    auth_headers,
    emit_fal_completed,
    emit_fal_error,
    grant_coins,
    job_input_payload,
    provider_request_id,
)

VIDEO_PRICE = 30
GRANT = 100


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


async def _job_row(app, job_id: str) -> Job:
    async with app.state.sessionmaker() as session:
        job = await session.get(Job, _uuid.UUID(job_id))
        assert job is not None
        session.expunge(job)
        return job


async def _balance(client, headers) -> dict:
    return (await client.get("/v1/billing/balance", headers=headers)).json()


async def _video_asset_meta(app, job_id: str) -> dict | None:
    async with app.state.sessionmaker() as session:
        stmt = (
            select(Asset)
            .where(Asset.kind == AssetKind.video)
            .where(Asset.meta["job_id"].astext == str(job_id))
            .limit(1)
        )
        asset = (await session.execute(stmt)).scalars().first()
        return dict(asset.meta or {}) if asset else None


class _Spy:
    """Оборачивает async-метод провайдера: пишет kwargs вызовов и делегирует оригиналу."""

    def __init__(self, target, name):
        self.calls: list[dict] = []
        self._orig = getattr(target, name)
        self._target = target
        self._name = name

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return await self._orig(**kwargs)

    def install(self):
        setattr(self._target, self._name, self)
        return self


async def _make_completed_song(client, app, headers, *, custom_lyrics: str) -> dict:
    """Создаёт завершённую песню (track+variant) с явной лирикой. Возвращает трек-JSON."""
    resp = await client.post(
        "/v1/songs",
        json={"prompt": "an upbeat indie pop song", "customLyrics": custom_lyrics},
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["jobId"]
    rid = await provider_request_id(app, job_id)
    wh = await emit_fal_completed(
        client, rid, media_url="https://cdn.local/song.mp3", duration=33.0
    )
    assert wh.json()["status"] == "ok"
    final = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
    assert final["status"] == "completed", final
    track = (await client.get(f"/v1/tracks/{final['trackId']}", headers=headers)).json()
    return track


# ==========================================================================
# avatar_performance
# ==========================================================================


@pytest.mark.asyncio
async def test_avatar_source_routes_to_lipsync(client, app):
    spy = _Spy(app.state.fal_provider, "submit_lipsync_video").install()
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
    job_id = resp.json()["jobId"]

    status = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
    assert status["currentStage"] == "lipsync"
    assert len(spy.calls) == 1
    assert spy.calls[0]["idempotency_key"] == f"{job_id}:lipsync"


@pytest.mark.asyncio
async def test_avatar_reference_routes_to_avatar_image(client, app):
    """Только референс-картинка (без source video) → submit_avatar_image_video,
    idempotency :avatar, current_stage lipsync; webhook completed → Asset + capture 30."""
    spy_img = _Spy(app.state.fal_provider, "submit_avatar_image_video").install()
    spy_lip = _Spy(app.state.fal_provider, "submit_lipsync_video").install()
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT)

    resp = await client.post(
        "/v1/videos",
        json={
            "mode": "avatar_performance",
            "audioUrl": "https://cdn.local/song.mp3",
            "referenceImageUrl": "https://cdn.local/face.png",
            "style": "realistic",
        },
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["jobId"]

    status = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
    assert status["currentStage"] == "lipsync"
    assert len(spy_img.calls) == 1
    assert len(spy_lip.calls) == 0
    assert spy_img.calls[0]["idempotency_key"] == f"{job_id}:avatar"

    rid = await provider_request_id(app, job_id)
    wh = await emit_fal_completed(
        client, rid, media_url="https://cdn.local/avatar-result.mp4", duration=40.0
    )
    assert wh.json()["status"] == "ok"

    meta = await _video_asset_meta(app, job_id)
    assert meta is not None
    assert meta["mode"] == "avatar_performance"
    assert meta["aspect_ratio"] == "9:16"
    assert meta["style"] == "realistic"
    bal = await _balance(client, headers)
    assert bal == {"coinsAvailable": GRANT - VIDEO_PRICE, "coinsReserved": 0}


# ==========================================================================
# visual_clip
# ==========================================================================


@pytest.mark.asyncio
async def test_visual_clip_stage_and_degraded_mux(client, app):
    spy_t2v = _Spy(app.state.fal_provider, "submit_text_to_video").install()
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT)

    resp = await client.post(
        "/v1/videos",
        json={
            "mode": "visual_clip",
            "audioUrl": "https://cdn.local/song.mp3",
            "prompt": "aurora over mountains",
            "style": "cinematic",
        },
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["jobId"]

    status = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
    assert status["currentStage"] == "visual_gen"
    assert len(spy_t2v.calls) == 1
    assert spy_t2v.calls[0]["idempotency_key"] == f"{job_id}:visual"

    rid = await provider_request_id(app, job_id)
    wh = await emit_fal_completed(
        client, rid, media_url="https://cdn.local/clip.mp4", duration=6.0
    )
    assert wh.json()["status"] == "ok"

    video = (await client.get(f"/v1/videos/{job_id}", headers=headers)).json()
    assert video["status"] == "completed"
    meta = await _video_asset_meta(app, job_id)
    assert meta["mode"] == "visual_clip"
    assert meta["style"] == "cinematic"
    assert meta.get("quality_flag") == "muted_no_ffmpeg"  # деградация без ffmpeg
    bal = await _balance(client, headers)
    assert bal == {"coinsAvailable": GRANT - VIDEO_PRICE, "coinsReserved": 0}


@pytest.mark.asyncio
async def test_visual_clip_with_reference_routes_to_i2v(client, app):
    spy_i2v = _Spy(app.state.fal_provider, "submit_image_to_video").install()
    spy_t2v = _Spy(app.state.fal_provider, "submit_text_to_video").install()
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT)

    resp = await client.post(
        "/v1/videos",
        json={
            "mode": "visual_clip",
            "audioUrl": "https://cdn.local/song.mp3",
            "prompt": "slow zoom",
            "referenceImageUrl": "https://cdn.local/ref.png",
        },
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    assert len(spy_i2v.calls) == 1
    assert len(spy_t2v.calls) == 0


# ==========================================================================
# lyrics_video (АСИНХРОННЫЙ; start НЕ блокирует — рендер в advance)
# ==========================================================================


@pytest.mark.asyncio
async def test_lyrics_video_start_nonblocking(client, app):
    """POST → 202 быстро: start() делает ТОЛЬКО fal-submit t2v-фона. provider_request_id
    выставлен, current_stage=visual_gen, статус running, рендер НЕ выполнялся."""
    spy_bg = _Spy(app.state.fal_provider, "submit_lyrics_background").install()
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT)

    resp = await client.post(
        "/v1/videos",
        json={
            "mode": "lyrics_video",
            "audioUrl": "https://cdn.local/song.mp3",
            "lyrics": "line one\nline two\nline three",
        },
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["jobId"]

    status = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
    assert status["status"] == "running"  # start не финализировал/не провалил
    assert status["currentStage"] == "visual_gen"

    job = await _job_row(app, job_id)
    assert job.provider_request_id is not None
    # инвариант: submit ушёл на lyrics-bg модель (не t2v)
    assert len(spy_bg.calls) == 1
    assert spy_bg.calls[0]["idempotency_key"] == f"{job_id}:lyrics_bg"
    assert job.provider_model == get_settings().FAL_VIDEO_LYRICS_BG_MODEL


@pytest.mark.asyncio
async def test_lyrics_video_render_without_ffmpeg_fails_and_releases(client, app):
    """webhook completed(visual_gen) → _render_lyrics. ffmpeg недоступен → job FAILED
    и монеты возвращены (release). НЕ succeeded с битым видео (ADR-007 §3)."""
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT)

    resp = await client.post(
        "/v1/videos",
        json={
            "mode": "lyrics_video",
            "audioUrl": "https://cdn.local/song.mp3",
            "lyrics": "sing this line",
        },
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["jobId"]

    rid = await provider_request_id(app, job_id)
    wh = await emit_fal_completed(
        client, rid, media_url="https://cdn.local/bg.mp4", duration=10.0
    )
    assert wh.json()["status"] == "ok"

    final = (await client.get(f"/v1/videos/{job_id}", headers=headers)).json()
    assert final["status"] == "failed"
    assert final["videoUrl"] is None
    # видео-Asset не создан
    assert await _video_asset_meta(app, job_id) is None
    # release: монеты вернулись в кошелёк
    bal = await _balance(client, headers)
    assert bal == {"coinsAvailable": GRANT, "coinsReserved": 0}


# ==========================================================================
# Валидатор режимов (схема pydantic → 400 INVALID_INPUT; unknown variant → 422)
# ==========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        # avatar без source/reference
        {"mode": "avatar_performance", "audioUrl": "https://cdn.local/a.mp3"},
        # visual без prompt и surpriseMe
        {"mode": "visual_clip", "audioUrl": "https://cdn.local/a.mp3"},
        # lyrics без lyrics и trackId
        {"mode": "lyrics_video", "audioUrl": "https://cdn.local/a.mp3"},
        # ни одного источника аудио
        {"mode": "visual_clip", "prompt": "x"},
        # source_video на не-avatar режиме
        {
            "mode": "visual_clip",
            "audioUrl": "https://cdn.local/a.mp3",
            "prompt": "x",
            "sourceVideoUrl": "https://cdn.local/v.mp4",
        },
    ],
)
async def test_video_mode_validator_rejects(client, body):
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT)
    resp = await client.post("/v1/videos", json=body, headers=headers)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_video_ambiguous_audio_source_rejected(client, app):
    """audioUrl И trackId одновременно → 400 (валидатор ambiguous audio source)."""
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT)
    track = await _make_completed_song(client, app, headers, custom_lyrics="la la")
    resp = await client.post(
        "/v1/videos",
        json={
            "mode": "visual_clip",
            "prompt": "x",
            "audioUrl": "https://cdn.local/a.mp3",
            "trackId": track["id"],
        },
        headers=headers,
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "INVALID_INPUT"


# ==========================================================================
# provider_model / model desync guard
# ==========================================================================


@pytest.mark.asyncio
async def test_provider_model_matches_called_model_per_mode(client, app):
    """job.provider_model совпадает с реально вызванной моделью для каждого режима."""
    s = get_settings()
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT + GRANT)  # хватит на несколько job

    async def _mk(body) -> Job:
        r = await client.post("/v1/videos", json=body, headers=headers)
        assert r.status_code == 202, r.text
        return await _job_row(app, r.json()["jobId"])

    lyrics_job = await _mk(
        {"mode": "lyrics_video", "audioUrl": "https://cdn.local/a.mp3", "lyrics": "hi"}
    )
    visual_job = await _mk(
        {"mode": "visual_clip", "audioUrl": "https://cdn.local/a.mp3", "prompt": "p"}
    )
    avatar_job = await _mk(
        {
            "mode": "avatar_performance",
            "audioUrl": "https://cdn.local/a.mp3",
            "sourceVideoUrl": "https://cdn.local/v.mp4",
        }
    )
    assert lyrics_job.provider_model == s.FAL_VIDEO_LYRICS_BG_MODEL
    assert visual_job.provider_model == s.FAL_VIDEO_VISUAL_MODEL
    assert avatar_job.provider_model == s.FAL_VIDEO_AVATAR_MODEL


@pytest.mark.asyncio
async def test_lyrics_desync_uses_bg_model_not_visual(client, app, monkeypatch):
    """При FAL_VIDEO_LYRICS_BG_MODEL != FAL_VIDEO_VISUAL_MODEL: lyrics submit ведётся
    по lyrics-bg модели, visual — по visual. Проверяем через provider_model job'а
    и через маршрут submit-метода стаба."""
    s = get_settings()
    monkeypatch.setattr(s, "FAL_VIDEO_LYRICS_BG_MODEL", "vendor/lyrics-bg-distinct")
    assert s.FAL_VIDEO_LYRICS_BG_MODEL != s.FAL_VIDEO_VISUAL_MODEL

    spy_bg = _Spy(app.state.fal_provider, "submit_lyrics_background").install()
    spy_t2v = _Spy(app.state.fal_provider, "submit_text_to_video").install()

    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT + GRANT)

    r_lyr = await client.post(
        "/v1/videos",
        json={"mode": "lyrics_video", "audioUrl": "https://cdn.local/a.mp3", "lyrics": "x"},
        headers=headers,
    )
    assert r_lyr.status_code == 202, r_lyr.text
    lyr_job = await _job_row(app, r_lyr.json()["jobId"])

    r_vis = await client.post(
        "/v1/videos",
        json={"mode": "visual_clip", "audioUrl": "https://cdn.local/a.mp3", "prompt": "p"},
        headers=headers,
    )
    assert r_vis.status_code == 202, r_vis.text
    vis_job = await _job_row(app, r_vis.json()["jobId"])

    assert lyr_job.provider_model == "vendor/lyrics-bg-distinct"
    assert vis_job.provider_model == s.FAL_VIDEO_VISUAL_MODEL
    assert lyr_job.provider_model != vis_job.provider_model
    # маршрут submit: lyrics → lyrics_background, visual → text_to_video
    assert len(spy_bg.calls) == 1 and len(spy_t2v.calls) == 1


# ==========================================================================
# POST /v1/uploads/image
# ==========================================================================


@pytest.mark.asyncio
async def test_upload_image_accepts_png(client):
    headers = await auth_headers(client)
    resp = await client.post(
        "/v1/uploads/image",
        files={"file": ("ref.png", b"\x89PNG\r\n\x1a\n" + b"0" * 32, "image/png")},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text  # синхронная загрузка, echo ресурса
    body = resp.json()
    assert body["mime"] == "image/png"
    assert body["kind"] == "image"
    assert body["url"]


@pytest.mark.asyncio
async def test_upload_image_rejects_audio(client):
    headers = await auth_headers(client)
    resp = await client.post(
        "/v1/uploads/image",
        files={"file": ("song.mp3", b"ID3" + b"0" * 32, "audio/mpeg")},
        headers=headers,
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["details"]["reason"] == "unsupported_content_type"


# ==========================================================================
# trackId-резолв «My track»
# ==========================================================================


@pytest.mark.asyncio
async def test_video_trackid_resolves_audio_and_lyrics(client, app):
    """lyrics_video по своему треку: audio_url резолвится из варианта, лирика — из
    задачи-песни (Job.input_payload['_lyrics'])."""
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT + GRANT)
    custom_lyrics = "verse one line\nchorus line here"
    track = await _make_completed_song(client, app, headers, custom_lyrics=custom_lyrics)
    audio_url = track["variants"][0]["audioUrl"]

    resp = await client.post(
        "/v1/videos",
        json={"mode": "lyrics_video", "trackId": track["id"]},
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["jobId"]

    payload = await job_input_payload(app, job_id)
    assert payload["audio_url"] == audio_url
    assert payload["lyrics"] == custom_lyrics


@pytest.mark.asyncio
async def test_video_trackid_foreign_track_404(client, app):
    headers_a = await auth_headers(client)
    await grant_coins(client, headers_a, coins=GRANT + GRANT)
    track = await _make_completed_song(client, app, headers_a, custom_lyrics="x")

    headers_b = await auth_headers(client)
    await grant_coins(client, headers_b, coins=GRANT)
    resp = await client.post(
        "/v1/videos",
        json={"mode": "visual_clip", "prompt": "p", "trackId": track["id"]},
        headers=headers_b,
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_video_trackid_unknown_variant_422(client, app):
    """Неизвестный variant_id → 422 (endpoint-level ValidationFailed, не схема)."""
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT + GRANT)
    track = await _make_completed_song(client, app, headers, custom_lyrics="x")
    resp = await client.post(
        "/v1/videos",
        json={
            "mode": "visual_clip",
            "prompt": "p",
            "trackId": track["id"],
            "variantId": str(_uuid.uuid4()),
        },
        headers=headers,
    )
    assert resp.status_code == 422, resp.text


# ==========================================================================
# Protocol conformance стаба
# ==========================================================================


def test_stub_implements_video_submit_methods():
    for name in (
        "submit_lyrics_background",
        "submit_text_to_video",
        "submit_image_to_video",
        "submit_avatar_image_video",
        "submit_lipsync_video",
    ):
        method = getattr(StubFalProvider, name, None)
        assert method is not None, f"StubFalProvider missing {name}"
        assert inspect.iscoroutinefunction(method), f"{name} must be async"


# ==========================================================================
# Инварианты монет: release при провале fal-фона (failed webhook)
# ==========================================================================


@pytest.mark.asyncio
async def test_video_provider_failed_releases_coins(client, app):
    """fal ERROR-конверт по видео-задаче → job failed + release (монеты возвращены)."""
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT)

    resp = await client.post(
        "/v1/videos",
        json={
            "mode": "visual_clip",
            "audioUrl": "https://cdn.local/song.mp3",
            "prompt": "p",
        },
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["jobId"]

    rid = await provider_request_id(app, job_id)
    wh = await emit_fal_error(client, rid, error="t2v backend failed")
    assert wh.json()["status"] == "ok"

    final = (await client.get(f"/v1/videos/{job_id}", headers=headers)).json()
    assert final["status"] == "failed"
    bal = await _balance(client, headers)
    assert bal == {"coinsAvailable": GRANT, "coinsReserved": 0}
