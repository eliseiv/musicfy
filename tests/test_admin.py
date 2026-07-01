from __future__ import annotations

import pytest

ADMIN = {"Authorization": "Bearer test-admin-key"}


async def _guest(client) -> tuple[str, dict]:
    r = (await client.post("/v1/auth/guest", json={})).json()
    return r["userId"], {"Authorization": f"Bearer {r['token']}"}


@pytest.mark.asyncio
async def test_admin_requires_admin_key(client):
    user_id, _ = await _guest(client)
    # без админ-ключа
    resp = await client.post(
        f"/v1/admin/users/{user_id}/credits", json={"coins": 5}
    )
    assert resp.status_code in (401, 403)
    # с пользовательским токеном (не админским) — тоже отказ
    _, uh = await _guest(client)
    resp2 = await client.post(
        f"/v1/admin/users/{user_id}/credits", json={"coins": 5}, headers=uh
    )
    assert resp2.status_code == 403
    # обычный сервисный API_KEY НЕ даёт админ-доступ (ключи разделены)
    resp3 = await client.post(
        f"/v1/admin/users/{user_id}/credits",
        json={"coins": 5},
        headers={"Authorization": "Bearer test-service-key"},
    )
    assert resp3.status_code == 403


@pytest.mark.asyncio
async def test_admin_grant_credits(client):
    user_id, uh = await _guest(client)
    resp = await client.post(
        f"/v1/admin/users/{user_id}/credits",
        json={"coins": 20, "reason": "support comp"},
        headers=ADMIN,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"userId": user_id, "coinsAvailable": 20, "coinsReserved": 0}

    # пользователь теперь может генерировать (хватает монет: song=10)
    job = await client.post("/v1/songs", json={"prompt": "x"}, headers=uh)
    assert job.status_code == 202


@pytest.mark.asyncio
async def test_admin_grant_subscription_credits_coins(client):
    user_id, uh = await _guest(client)
    resp = await client.post(
        f"/v1/admin/users/{user_id}/subscription",
        json={"coins": 150, "periodDays": 7, "label": "weekly"},
        headers=ADMIN,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"userId": user_id, "coinsAvailable": 150, "coinsReserved": 0}

    # видно и самому пользователю через /billing/balance
    bal = (await client.get("/v1/billing/balance", headers=uh)).json()
    assert bal == {"coinsAvailable": 150, "coinsReserved": 0}


@pytest.mark.asyncio
async def test_admin_balance_endpoint(client):
    user_id, _ = await _guest(client)
    await client.post(
        f"/v1/admin/users/{user_id}/credits", json={"coins": 42}, headers=ADMIN
    )
    resp = await client.get(f"/v1/admin/users/{user_id}/balance", headers=ADMIN)
    assert resp.status_code == 200
    assert resp.json() == {"userId": user_id, "coinsAvailable": 42, "coinsReserved": 0}


@pytest.mark.asyncio
async def test_admin_unknown_user_404(client):
    resp = await client.post(
        "/v1/admin/users/00000000-0000-0000-0000-000000000000/credits",
        json={"coins": 5},
        headers=ADMIN,
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "USER_NOT_FOUND"


@pytest.mark.asyncio
async def test_admin_revoke_subscription_keeps_coins(client):
    """Монеты non-expiring: revoke меняет статус, но монеты не сгорают."""
    user_id, uh = await _guest(client)
    await client.post(
        f"/v1/admin/users/{user_id}/subscription",
        json={"coins": 150, "periodDays": 7}, headers=ADMIN,
    )
    revoke = await client.request(
        "DELETE", f"/v1/admin/users/{user_id}/subscription", headers=ADMIN
    )
    assert revoke.status_code == 200
    # монеты сохранены после отзыва подписки
    bal = (await client.get("/v1/billing/balance", headers=uh)).json()
    assert bal == {"coinsAvailable": 150, "coinsReserved": 0}


# --------------------------------------------------------------------------
# PATCH /admin/pricing/{jobType}
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_set_price_changes_reservation(client):
    """Изменение цены через PATCH применяется к следующей генерации."""
    user_id, uh = await _guest(client)
    await client.post(
        f"/v1/admin/users/{user_id}/credits", json={"coins": 100}, headers=ADMIN
    )
    # song: 10 → 25
    patch = await client.patch(
        "/v1/admin/pricing/song", json={"priceCoins": 25, "active": True}, headers=ADMIN
    )
    assert patch.status_code == 200, patch.text
    assert patch.json() == {"jobType": "song", "priceCoins": 25, "active": True}

    # прайс-лист отражает новую цену
    pricing = (await client.get("/v1/billing/pricing")).json()
    prices = {p["jobType"]: p["priceCoins"] for p in pricing["prices"]}
    assert prices["song"] == 25

    # следующая генерация резервирует 25
    await client.post("/v1/songs", json={"prompt": "x"}, headers=uh)
    bal = (await client.get("/v1/billing/balance", headers=uh)).json()
    assert bal == {"coinsAvailable": 75, "coinsReserved": 25}


@pytest.mark.asyncio
async def test_admin_set_price_invalid_job_type_400(client):
    resp = await client.patch(
        "/v1/admin/pricing/not_a_type", json={"priceCoins": 5, "active": True}, headers=ADMIN
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_admin_inactive_price_not_reserved_nor_listed(client):
    """active=false: тип не в /pricing и не резервируется (генерация как бесплатная)."""
    user_id, uh = await _guest(client)
    # деактивируем cover
    patch = await client.patch(
        "/v1/admin/pricing/cover", json={"priceCoins": 5, "active": False}, headers=ADMIN
    )
    assert patch.status_code == 200
    assert patch.json()["active"] is False

    # cover исчез из активного прайс-листа
    pricing = (await client.get("/v1/billing/pricing")).json()
    assert "cover" not in {p["jobType"] for p in pricing["prices"]}

    # генерация cover проходит без монет (неактивная цена → 0, резерва нет)
    resp = await client.post(
        "/v1/covers", json={"source_audio_url": "https://cdn.local/in.mp3"}, headers=uh
    )
    assert resp.status_code == 202, resp.text
    bal = (await client.get("/v1/billing/balance", headers=uh)).json()
    assert bal == {"coinsAvailable": 0, "coinsReserved": 0}
