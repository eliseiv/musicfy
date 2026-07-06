from __future__ import annotations

import uuid as _uuid

import pytest
from sqlalchemy import text

from tests.helpers import auth_headers

# Стартовый каталог из миграции 0012 (ADR-006), порядок по sort_order.
EXPECTED_ORDER = ["aria", "max", "luna", "kai", "nova", "leo", "sage", "rex"]


@pytest.mark.asyncio
async def test_presets_voices_returns_catalog_camel_case(client):
    headers = await auth_headers(client)
    resp = await client.get("/v1/presets/voices", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) >= 8

    keys = {item["key"] for item in body}
    for expected_key in ("aria", "max", "luna", "kai"):
        assert expected_key in keys, f"{expected_key} отсутствует в каталоге"

    # camelCase-контракт PresetVoiceView: previewUrl / sampleDurationSeconds.
    first = body[0]
    assert "previewUrl" in first
    assert "sampleDurationSeconds" in first
    assert "preview_url" not in first
    assert "sample_duration_seconds" not in first
    # Превью забэкфилены миграцией 0014 (реальные fal-URL) → непустые.
    assert isinstance(first["previewUrl"], str) and first["previewUrl"]
    assert isinstance(first["sampleDurationSeconds"], int)


@pytest.mark.asyncio
async def test_presets_voices_never_exposes_provider_voice(client):
    headers = await auth_headers(client)
    resp = await client.get("/v1/presets/voices", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body, "каталог не должен быть пустым"
    for item in body:
        # Провайдерский идентификатор голоса наружу не отдаётся (ADR-006).
        assert "providerVoice" not in item
        assert "provider_voice" not in item


@pytest.mark.asyncio
async def test_presets_voices_ordered_by_sort_order(client):
    headers = await auth_headers(client)
    resp = await client.get("/v1/presets/voices", headers=headers)
    assert resp.status_code == 200, resp.text
    keys = [item["key"] for item in resp.json()]
    # Относительный порядок известных пресетов должен совпадать с sort_order.
    known = [k for k in keys if k in EXPECTED_ORDER]
    assert known == EXPECTED_ORDER


@pytest.mark.asyncio
async def test_presets_voices_active_only(client, app):
    headers = await auth_headers(client)
    inactive_key = f"zz_inactive_{_uuid.uuid4().hex[:8]}"

    # Вставляем неактивный пресет напрямую (preset_voices не входит в TRUNCATE),
    # проверяем, что он не попадает в выдачу, и убираем за собой.
    async with app.state.sessionmaker() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO preset_voices "
                    "(key, title, provider_voice, active, sort_order) "
                    "VALUES (:key, :title, :pv, false, 999)"
                ),
                {"key": inactive_key, "title": "Inactive", "pv": "Rachel"},
            )
    try:
        resp = await client.get("/v1/presets/voices", headers=headers)
        assert resp.status_code == 200, resp.text
        keys = {item["key"] for item in resp.json()}
        assert inactive_key not in keys
    finally:
        async with app.state.sessionmaker() as session:
            async with session.begin():
                await session.execute(
                    text("DELETE FROM preset_voices WHERE key = :key"),
                    {"key": inactive_key},
                )
