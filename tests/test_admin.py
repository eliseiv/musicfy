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
        f"/v1/admin/users/{user_id}/credits", json={"category": "song", "amount": 5}
    )
    assert resp.status_code in (401, 403)
    # с пользовательским токеном (не админским) — тоже отказ
    _, uh = await _guest(client)
    resp2 = await client.post(
        f"/v1/admin/users/{user_id}/credits", json={"category": "song", "amount": 5}, headers=uh
    )
    assert resp2.status_code == 403
    # обычный сервисный API_KEY НЕ даёт админ-доступ (ключи разделены)
    resp3 = await client.post(
        f"/v1/admin/users/{user_id}/credits",
        json={"category": "song", "amount": 5},
        headers={"Authorization": "Bearer test-service-key"},
    )
    assert resp3.status_code == 403


@pytest.mark.asyncio
async def test_admin_grant_credits(client):
    user_id, uh = await _guest(client)
    resp = await client.post(
        f"/v1/admin/users/{user_id}/credits",
        json={"category": "song", "amount": 7, "reason": "support comp"},
        headers=ADMIN,
    )
    assert resp.status_code == 200, resp.text
    song = next(b for b in resp.json()["balances"] if b["category"] == "song")
    assert song["purchasedAvailable"] == 7

    # пользователь теперь может генерировать (хватает кредитов)
    job = await client.post("/v1/songs", json={"prompt": "x"}, headers=uh)
    assert job.status_code == 202


@pytest.mark.asyncio
async def test_admin_grant_subscription(client):
    user_id, uh = await _guest(client)
    resp = await client.post(
        f"/v1/admin/users/{user_id}/subscription",
        json={"song": 30, "cover": 10, "video": 3, "periodDays": 7},
        headers=ADMIN,
    )
    assert resp.status_code == 200, resp.text
    balances = {b["category"]: b for b in resp.json()["balances"]}
    assert balances["song"]["subscriptionRemaining"] == 30
    assert balances["video"]["subscriptionGranted"] == 3

    # видно и самому пользователю
    bal = (await client.get("/v1/billing/balance", headers=uh)).json()
    song = next(b for b in bal["balances"] if b["category"] == "song")
    assert song["subscriptionRemaining"] == 30


@pytest.mark.asyncio
async def test_admin_unknown_user_404(client):
    resp = await client.post(
        "/v1/admin/users/00000000-0000-0000-0000-000000000000/credits",
        json={"category": "song", "amount": 5},
        headers=ADMIN,
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "USER_NOT_FOUND"


@pytest.mark.asyncio
async def test_admin_revoke_subscription(client):
    user_id, uh = await _guest(client)
    await client.post(
        f"/v1/admin/users/{user_id}/subscription",
        json={"song": 30, "periodDays": 7}, headers=ADMIN,
    )
    revoke = await client.request(
        "DELETE", f"/v1/admin/users/{user_id}/subscription", headers=ADMIN
    )
    assert revoke.status_code == 200
    # после отзыва подписочный остаток не доступен
    bal = (await client.get("/v1/billing/balance", headers=uh)).json()
    song = next(b for b in bal["balances"] if b["category"] == "song")
    assert song["subscriptionRemaining"] == 0
