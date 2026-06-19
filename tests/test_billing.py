from __future__ import annotations

import time

import pytest

from tests.helpers import auth_headers, make_signed_transaction


@pytest.mark.asyncio
async def test_paywall_without_credits(client):
    headers = await auth_headers(client)
    resp = await client.post("/v1/songs", json={"prompt": "x"}, headers=headers)
    assert resp.status_code == 402
    assert resp.json()["error"]["code"] == "INSUFFICIENT_CREDITS"


@pytest.mark.asyncio
async def test_products_listed(client):
    resp = await client.get("/v1/billing/products")
    assert resp.status_code == 200
    ids = {p["productId"] for p in resp.json()}
    assert "com.musicfy.sub.weekly" in ids
    assert "com.musicfy.pack.song" in ids


@pytest.mark.asyncio
async def test_subscription_grants_entitlements_and_spend(client):
    headers = await auth_headers(client)
    expires_ms = int((time.time() + 7 * 86400) * 1000)
    signed = make_signed_transaction(
        product_id="com.musicfy.sub.weekly", transaction_id="tx-weekly-1",
        expires_date_ms=expires_ms,
    )
    r = await client.post(
        "/v1/billing/purchases/verify", json={"signedTransaction": signed}, headers=headers
    )
    assert r.status_code == 200, r.text

    balance = (await client.get("/v1/billing/balance", headers=headers)).json()
    song = next(b for b in balance["balances"] if b["category"] == "song")
    assert song["subscriptionRemaining"] == 30

    # генерация резервирует 1 из подписки
    job = await client.post("/v1/songs", json={"prompt": "x"}, headers=headers)
    assert job.status_code == 202

    balance2 = (await client.get("/v1/billing/balance", headers=headers)).json()
    song2 = next(b for b in balance2["balances"] if b["category"] == "song")
    assert song2["subscriptionRemaining"] == 29


@pytest.mark.asyncio
async def test_purchase_pack_credits_and_dedup(client):
    headers = await auth_headers(client)
    signed = make_signed_transaction(
        product_id="com.musicfy.pack.song", transaction_id="tx-pack-1"
    )
    r1 = await client.post(
        "/v1/billing/purchases/verify", json={"signedTransaction": signed}, headers=headers
    )
    assert r1.json()["status"] == "ok"
    # повторная та же транзакция — без двойного начисления
    r2 = await client.post(
        "/v1/billing/purchases/verify", json={"signedTransaction": signed}, headers=headers
    )
    assert r2.json()["deduplicated"] is True

    balance = (await client.get("/v1/billing/balance", headers=headers)).json()
    song = next(b for b in balance["balances"] if b["category"] == "song")
    assert song["purchasedAvailable"] == 20

    ledger = (await client.get("/v1/billing/ledger", headers=headers)).json()
    assert any(e["kind"] == "credit_purchase" for e in ledger)


@pytest.mark.asyncio
async def test_guest_purchase_merges_into_apple_account(client, app):
    """Guest покупает пак, затем входит через Apple — кредиты не теряются (ТЗ 5.6)."""
    from app.auth.sessions import AuthService

    class FakeAppleVerifier:
        async def verify(self, identity_token, *, nonce=None):
            return {"sub": identity_token}

        async def aclose(self):
            pass

    app.state.auth_service = AuthService(
        app.state.sessionmaker, apple_verifier=FakeAppleVerifier(), session_ttl_seconds=3600
    )

    guest = (await client.post("/v1/auth/guest", json={})).json()
    gh = {"Authorization": f"Bearer {guest['token']}"}
    await client.post(
        "/v1/billing/purchases/verify",
        json={"signedTransaction": make_signed_transaction(
            product_id="com.musicfy.pack.song", transaction_id="tx-merge-1")},
        headers=gh,
    )

    apple = (
        await client.post(
            "/v1/auth/apple", json={"identityToken": "apple-merge-sub"}, headers=gh
        )
    ).json()
    ah = {"Authorization": f"Bearer {apple['token']}"}
    balance = (await client.get("/v1/billing/balance", headers=ah)).json()
    song = next(b for b in balance["balances"] if b["category"] == "song")
    assert song["purchasedAvailable"] == 20


@pytest.mark.asyncio
async def test_spend_order_subscription_before_purchase(client):
    """Сначала тратится подписка, потом покупные кредиты."""
    headers = await auth_headers(client)
    # подписка: 30 song
    await client.post(
        "/v1/billing/purchases/verify",
        json={"signedTransaction": make_signed_transaction(
            product_id="com.musicfy.sub.weekly", transaction_id="tx-w2",
            expires_date_ms=int((time.time() + 7 * 86400) * 1000),
        )},
        headers=headers,
    )
    # пак: +20 song purchased
    await client.post(
        "/v1/billing/purchases/verify",
        json={"signedTransaction": make_signed_transaction(
            product_id="com.musicfy.pack.song", transaction_id="tx-p2",
        )},
        headers=headers,
    )
    await client.post("/v1/songs", json={"prompt": "x"}, headers=headers)

    balance = (await client.get("/v1/billing/balance", headers=headers)).json()
    song = next(b for b in balance["balances"] if b["category"] == "song")
    assert song["subscriptionRemaining"] == 29  # списали с подписки
    assert song["purchasedAvailable"] == 20  # пак не тронут
