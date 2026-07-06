from __future__ import annotations

import pytest
from sqlalchemy import select

from app.domain.enums import AssetKind
from app.domain.models.asset import Asset
from tests.helpers import (
    auth_headers,
    emit_fal_completed,
    grant_coins,
    provider_request_id,
)

# Видео стоит 30 монет (единый прайс-лист). Грант в helpers — 100 монет.
VIDEO_PRICE = 30
GRANT = 100


async def _balance(client, headers) -> dict:
    return (await client.get("/v1/billing/balance", headers=headers)).json()


async def _video_asset_meta(app, job_id: str) -> dict | None:
    """Возвращает meta видео-Asset'а, привязанного к job (или None, если не создан)."""
    async with app.state.sessionmaker() as session:
        stmt = (
            select(Asset)
            .where(Asset.kind == AssetKind.video)
            .where(Asset.meta["job_id"].astext == str(job_id))
            .limit(1)
        )
        asset = (await session.execute(stmt)).scalars().first()
        return dict(asset.meta or {}) if asset else None


# --------------------------------------------------------------------------
# happy-path режимов (стаб fal; реальный fal не дёргается)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_video_avatar_source_happy_path_e2e(client, app):
    """avatar_performance + source video → lipsync-модель; webhook completed →
    Asset video + capture 30 монет (аудио уже вшито моделью, мукс не нужен)."""
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT)

    # push-токен (проверяем, что финализация с уведомлением не падает)
    await client.post("/v1/devices/push-token", json={"token": "apns-token-xyz"}, headers=headers)

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

    status = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
    assert status["currentStage"] == "lipsync"

    # резерв отражён в кошельке (30 в reserved)
    bal = await _balance(client, headers)
    assert bal == {"coinsAvailable": GRANT - VIDEO_PRICE, "coinsReserved": VIDEO_PRICE}

    rid = await provider_request_id(app, job_id)
    wh = await emit_fal_completed(
        client, rid, media_url="https://cdn.local/result.mp4", duration=42.0
    )
    assert wh.json()["status"] == "ok"

    video = (await client.get(f"/v1/videos/{job_id}", headers=headers)).json()
    assert video["status"] == "completed"
    assert video["videoUrl"] == "https://cdn.local/result.mp4"
    assert video["mode"] == "avatar_performance"
    assert video["aspectRatio"] == "9:16"  # дефолт
    assert video["style"] == "cinematic"

    # capture 30: reserved вернулся в 0, available остался 70
    bal = await _balance(client, headers)
    assert bal == {"coinsAvailable": GRANT - VIDEO_PRICE, "coinsReserved": 0}


@pytest.mark.asyncio
async def test_video_visual_clip_happy_path_e2e(client, app):
    """visual_clip → visual_gen; webhook completed → mux_audio. В тест-среде ffmpeg
    отсутствует → деградация (quality_flag='muted_no_ffmpeg'), но job succeeded + capture 30."""
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT)

    resp = await client.post(
        "/v1/videos",
        json={
            "mode": "visual_clip",
            "audioUrl": "https://cdn.local/song.mp3",
            "prompt": "neon city flythrough",
            "style": "anime",
            "aspectRatio": "1:1",
        },
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["jobId"]

    status = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
    assert status["currentStage"] == "visual_gen"

    rid = await provider_request_id(app, job_id)
    wh = await emit_fal_completed(
        client, rid, media_url="https://cdn.local/clip.mp4", duration=6.0
    )
    assert wh.json()["status"] == "ok"

    video = (await client.get(f"/v1/videos/{job_id}", headers=headers)).json()
    assert video["status"] == "completed"
    assert video["videoUrl"]  # деградация возвращает исходный клип
    assert video["mode"] == "visual_clip"
    assert video["aspectRatio"] == "1:1"
    assert video["style"] == "anime"

    meta = await _video_asset_meta(app, job_id)
    assert meta is not None
    assert meta["mode"] == "visual_clip"
    # ffmpeg недоступен → мукс деградировал (немое видео), но job не провален
    assert meta.get("quality_flag") == "muted_no_ffmpeg"

    # capture 30
    bal = await _balance(client, headers)
    assert bal == {"coinsAvailable": GRANT - VIDEO_PRICE, "coinsReserved": 0}


# --------------------------------------------------------------------------
# Paywall: валидное тело, но нет монет → 402
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_video_requires_credits(client):
    """Валидное тело (mode задан), но пустой кошелёк → 402 InsufficientCoins.

    Важно: тело валидно по схеме — иначе отказ был бы 400 (INVALID_INPUT) до резерва.
    """
    headers = await auth_headers(client)
    resp = await client.post(
        "/v1/videos",
        json={
            "mode": "avatar_performance",
            "audioUrl": "https://cdn.local/a.mp3",
            "sourceVideoUrl": "https://cdn.local/v.mp4",
        },
        headers=headers,
    )
    assert resp.status_code == 402, resp.text


@pytest.mark.asyncio
async def test_video_missing_mode_is_rejected(client):
    """Старый контракт (без mode) больше не принимается: mode обязателен → 400 INVALID_INPUT."""
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT)
    resp = await client.post(
        "/v1/videos",
        json={"audioUrl": "https://cdn.local/a.mp3", "sourceVideoUrl": "https://cdn.local/v.mp4"},
        headers=headers,
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "INVALID_INPUT"
