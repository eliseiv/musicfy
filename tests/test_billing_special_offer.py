"""Спецоффер `offer_week_3.99_nottrial` (миграция 0022).

Недельная подписка по сниженной цене с тем же грантом, что и `week_6.99_not_trial` — 700
монет за 7 дней. Цена хранится в App Store Connect, у нас только размер начисления.
"""
from __future__ import annotations

import time
import uuid

import pytest
from sqlalchemy import text

from tests.helpers import auth_headers, auth_user, make_signed_transaction

_OFFER = "offer_week_3.99_nottrial"
_ADAPTY_URL = "/v1/billing/adapty/webhook"
_ADAPTY_AUTH = {"Authorization": "Bearer test-adapty-secret"}


async def _balance(client, headers) -> int:
    return (await client.get("/v1/billing/balance", headers=headers)).json()[
        "coinsAvailable"
    ]


@pytest.mark.asyncio
async def test_offer_present_in_products_catalog(client):
    """Продукт виден клиенту в витрине с корректными полями."""
    products = {p["productId"]: p for p in (await client.get("/v1/billing/products")).json()}
    assert _OFFER in products, sorted(products)
    offer = products[_OFFER]
    assert offer["kind"] == "subscription"
    assert offer["grants"] == {"coins": 700}
    assert offer["periodDays"] == 7


@pytest.mark.asyncio
async def test_offer_grant_matches_regular_weekly(client):
    """Грант совпадает с обычной недельной подпиской — отличается только цена в App Store."""
    products = {p["productId"]: p for p in (await client.get("/v1/billing/products")).json()}
    assert products[_OFFER]["grants"] == products["week_6.99_not_trial"]["grants"]
    assert products[_OFFER]["periodDays"] == products["week_6.99_not_trial"]["periodDays"]


@pytest.mark.asyncio
async def test_storekit_purchase_credits_700(client):
    """StoreKit-путь: покупка спецоффера начисляет 700 монет."""
    headers = await auth_headers(client)
    before = await _balance(client, headers)

    resp = await client.post(
        "/v1/billing/purchases/verify",
        json={
            "signedTransaction": make_signed_transaction(
                product_id=_OFFER,
                transaction_id=f"tx-offer-{uuid.uuid4()}",
                expires_date_ms=int((time.time() + 7 * 86400) * 1000),
            )
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok", resp.json()
    assert await _balance(client, headers) - before == 700


@pytest.mark.asyncio
async def test_adapty_webhook_credits_700(client, app):
    """Adapty-путь: подписка активируется, монеты — из каталога, а не fallback (1000)."""
    user_id, headers = await auth_user(client)
    before = await _balance(client, headers)

    r = await client.post(
        _ADAPTY_URL,
        json={
            "event_type": "subscription_started",
            "event_properties": {
                "profile_event_id": str(uuid.uuid4()),
                "customer_user_id": user_id,
                "vendor_product_id": _OFFER,
                "transaction_id": 3990001,
            },
        },
        headers=_ADAPTY_AUTH,
    )
    assert r.json()["result"] == "applied"
    assert await _balance(client, headers) - before == 700

    async with app.state.sessionmaker() as session:
        row = (
            await session.execute(
                text(
                    "SELECT product_external_id, status::text FROM subscription_state "
                    "WHERE user_id = :u"
                ),
                {"u": user_id},
            )
        ).first()
    assert row is not None
    assert row[0] == _OFFER
    assert row[1] == "active"


@pytest.mark.asyncio
async def test_underscore_variant_is_not_resolved(client):
    """`offer_week_3.99_not_trial` (с подчёркиванием) — ДРУГОЙ продукт, его в каталоге нет.

    Резолв регистронезависим (ADR-021), но не «пунктуация-независим»: написание `nottrial`
    слитно — вербатим из App Store Connect, и расхождение обязано давать unknown_product,
    а не молча начислять монеты по похожему id.
    """
    headers = await auth_headers(client)
    before = await _balance(client, headers)

    resp = await client.post(
        "/v1/billing/purchases/verify",
        json={
            "signedTransaction": make_signed_transaction(
                product_id="offer_week_3.99_not_trial",
                transaction_id=f"tx-offer-bad-{uuid.uuid4()}",
            )
        },
        headers=headers,
    )
    assert resp.json()["reason"] == "unknown_product"
    assert await _balance(client, headers) == before


@pytest.mark.asyncio
async def test_offer_resolves_case_insensitively(client):
    """ADR-021 распространяется и на новый продукт."""
    headers = await auth_headers(client)
    before = await _balance(client, headers)

    resp = await client.post(
        "/v1/billing/purchases/verify",
        json={
            "signedTransaction": make_signed_transaction(
                product_id="OFFER_WEEK_3.99_NOTTRIAL",
                transaction_id=f"tx-offer-case-{uuid.uuid4()}",
                expires_date_ms=int((time.time() + 7 * 86400) * 1000),
            )
        },
        headers=headers,
    )
    assert resp.json()["status"] == "ok"
    assert await _balance(client, headers) - before == 700


@pytest.mark.asyncio
async def test_regular_weekly_still_works(client):
    """Регресс-страховка: обычная недельная подписка не задета."""
    headers = await auth_headers(client)
    before = await _balance(client, headers)

    resp = await client.post(
        "/v1/billing/purchases/verify",
        json={
            "signedTransaction": make_signed_transaction(
                product_id="week_6.99_not_trial",
                transaction_id=f"tx-week-{uuid.uuid4()}",
                expires_date_ms=int((time.time() + 7 * 86400) * 1000),
            )
        },
        headers=headers,
    )
    assert resp.json()["status"] == "ok"
    assert await _balance(client, headers) - before == 700
