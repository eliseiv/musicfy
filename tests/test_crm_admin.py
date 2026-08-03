from __future__ import annotations

import pytest

from tests.helpers import auth_user

ADMIN = {"X-Admin-Key": "test-admin-key"}


@pytest.mark.asyncio
async def test_crm_admin_auth_header_contract(client):
    user_id, _ = await auth_user(client)
    no_key = await client.get("/v1/admin/users")
    assert no_key.status_code == 403

    bad_key = await client.get("/v1/admin/users", headers={"X-Admin-Key": "wrong"})
    assert bad_key.status_code == 401

    ok = await client.get("/v1/admin/users", headers=ADMIN)
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_health_alias_for_crm(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_crm_list_and_detail_user(client):
    user_id, _ = await auth_user(client)
    listed = await client.get("/v1/admin/users", headers=ADMIN)
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] >= 1
    assert body["items"][0]["registered_at"].endswith("Z")

    detail = await client.get(f"/v1/admin/users/{user_id}", headers=ADMIN)
    assert detail.status_code == 200
    card = detail.json()
    assert card["id"] == user_id
    assert card["balance"]["tokens"] == 0
    assert card["subscription"]["active"] is False
    assert card["revenue"] is None
    assert card["media_stats"]["photos"]["total"] == 0


@pytest.mark.asyncio
async def test_crm_adjust_tokens_and_subscription(client):
    user_id, _ = await auth_user(client)

    grant = await client.post(
        f"/v1/admin/users/{user_id}/tokens",
        json={"amount": 50},
        headers=ADMIN,
    )
    assert grant.status_code == 200
    assert grant.json() == {"id": user_id, "tokens": 50}

    deduct = await client.post(
        f"/v1/admin/users/{user_id}/tokens",
        json={"amount": -10},
        headers=ADMIN,
    )
    assert deduct.status_code == 200
    assert deduct.json()["tokens"] == 40

    overdraft = await client.post(
        f"/v1/admin/users/{user_id}/tokens",
        json={"amount": -100},
        headers=ADMIN,
    )
    assert overdraft.status_code == 400
    assert overdraft.json()["detail"] == "insufficient balance"

    sub = await client.post(
        f"/v1/admin/users/{user_id}/subscription",
        json={
            "product_id": "week_6.99_not_trial",
            "expires_in_days": 7,
            "grant_id": "crm-grant-1",
        },
        headers=ADMIN,
    )
    assert sub.status_code == 200, sub.text
    sub_body = sub.json()
    assert sub_body["applied"] is True
    assert sub_body["subscription_active"] is True
    assert sub_body["tokens"] == 140  # 40 + 100 coins from weekly plan
    assert sub_body["subscription_expires_at"].endswith("Z")

    repeat = await client.post(
        f"/v1/admin/users/{user_id}/subscription",
        json={
            "product_id": "week_6.99_not_trial",
            "expires_in_days": 7,
            "grant_id": "crm-grant-1",
        },
        headers=ADMIN,
    )
    assert repeat.status_code == 200
    assert repeat.json()["applied"] is False
    assert repeat.json()["tokens"] == 140


@pytest.mark.asyncio
async def test_crm_products_stats_and_empty_histories(client):
    user_id, _ = await auth_user(client)

    products = await client.get("/v1/admin/products", headers=ADMIN)
    assert products.status_code == 200
    items = products.json()["items"]
    assert any(p["product_id"] == "week_6.99_not_trial" for p in items)

    stats = await client.get("/v1/admin/stats", headers=ADMIN)
    assert stats.status_code == 200
    assert stats.json()["users_total"] >= 1

    payments = await client.get(f"/v1/admin/users/{user_id}/payments", headers=ADMIN)
    assert payments.status_code == 200
    assert payments.json() == {"total": 0, "items": []}

    requests = await client.get(f"/v1/admin/users/{user_id}/requests", headers=ADMIN)
    assert requests.status_code == 200
    assert requests.json() == {"total": 0, "items": []}

    missing = await client.get(
        "/v1/admin/users/00000000-0000-0000-0000-000000000000",
        headers=ADMIN,
    )
    assert missing.status_code == 404
    assert missing.json()["detail"] == "User not found"
