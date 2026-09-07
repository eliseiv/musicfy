"""Регистронезависимый резолв продукта каталога (ADR-021).

Один и тот же по смыслу продукт заведён в App Store Connect в разном регистре у разных
приложений (`100_tokens_9.99` на zavionix, `100_Tokens_9.99` на doravixo), а каталог
`products` сеется общей миграцией. Точное сравнение давало бы `unknown_product` и потерю
монет на инстансе, чей регистр разошёлся с сидом — дефект из ADR-015.
"""
from __future__ import annotations

import time
import uuid

import pytest
from sqlalchemy import text

from tests.helpers import auth_headers, make_signed_transaction

_ADAPTY_URL = "/v1/billing/adapty/webhook"
_ADAPTY_AUTH = {"Authorization": "Bearer test-adapty-secret"}


async def _balance(client, headers) -> int:
    return (await client.get("/v1/billing/balance", headers=headers)).json()[
        "coinsAvailable"
    ]


# --- StoreKit-путь ---------------------------------------------------------


@pytest.mark.asyncio
async def test_storekit_uppercase_product_id_credits_wallet(client):
    """Чек с `100_Tokens_9.99` начисляет монеты по записи каталога `100_tokens_9.99`."""
    headers = await auth_headers(client)
    before = await _balance(client, headers)

    resp = await client.post(
        "/v1/billing/purchases/verify",
        json={
            "signedTransaction": make_signed_transaction(
                product_id="100_Tokens_9.99", transaction_id=f"tx-upper-{uuid.uuid4()}"
            )
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok", resp.json()
    assert await _balance(client, headers) - before == 100


@pytest.mark.asyncio
async def test_storekit_lowercase_still_works(client):
    """Регресс-страховка: исходный регистр продолжает начислять как раньше."""
    headers = await auth_headers(client)
    before = await _balance(client, headers)

    resp = await client.post(
        "/v1/billing/purchases/verify",
        json={
            "signedTransaction": make_signed_transaction(
                product_id="100_tokens_9.99", transaction_id=f"tx-lower-{uuid.uuid4()}"
            )
        },
        headers=headers,
    )
    assert resp.json()["status"] == "ok"
    assert await _balance(client, headers) - before == 100


@pytest.mark.asyncio
async def test_storekit_uppercase_subscription_credits_its_tier(client):
    """Подписка в другом регистре даёт свой грант, а не fallback."""
    headers = await auth_headers(client)
    before = await _balance(client, headers)

    resp = await client.post(
        "/v1/billing/purchases/verify",
        json={
            "signedTransaction": make_signed_transaction(
                product_id="WEEK_6.99_NOT_TRIAL",
                transaction_id=f"tx-sub-{uuid.uuid4()}",
                expires_date_ms=int((time.time() + 7 * 86400) * 1000),
            )
        },
        headers=headers,
    )
    assert resp.json()["status"] == "ok"
    assert await _balance(client, headers) - before == 700


@pytest.mark.asyncio
async def test_truly_unknown_product_still_ignored(client):
    """Регистронезависимость не должна превращать неизвестный продукт в начисление."""
    headers = await auth_headers(client)
    before = await _balance(client, headers)

    resp = await client.post(
        "/v1/billing/purchases/verify",
        json={
            "signedTransaction": make_signed_transaction(
                product_id="totally_unknown_sku", transaction_id=f"tx-unk-{uuid.uuid4()}"
            )
        },
        headers=headers,
    )
    assert resp.json() == {
        "status": "ignored",
        "deduplicated": False,
        "reason": "unknown_product",
    }
    assert await _balance(client, headers) == before


# --- Adapty-путь -----------------------------------------------------------


@pytest.mark.asyncio
async def test_adapty_uppercase_vendor_product_id_uses_catalog_tier(client):
    """Вебхук с `WEEK_6.99_NOT_TRIAL` берёт грант каталога (700), а не fallback (1000)."""
    from tests.helpers import auth_user

    user_id, headers = await auth_user(client)
    before = await _balance(client, headers)

    r = await client.post(
        _ADAPTY_URL,
        json={
            "event_type": "subscription_started",
            "event_properties": {
                "profile_event_id": str(uuid.uuid4()),
                "customer_user_id": user_id,
                "vendor_product_id": "WEEK_6.99_NOT_TRIAL",
                "transaction_id": 770001,
            },
        },
        headers=_ADAPTY_AUTH,
    )
    assert r.json()["result"] == "applied"
    assert await _balance(client, headers) - before == 700


@pytest.mark.asyncio
async def test_adapty_subscription_state_stores_catalog_id(client, app):
    """В `subscription_state` пишется id ИЗ КАТАЛОГА, а не присланный регистр.

    Иначе одна и та же подписка выглядела бы по-разному в зависимости от того, каким
    контуром пришло событие.
    """
    from tests.helpers import auth_user

    user_id, _ = await auth_user(client)
    await client.post(
        _ADAPTY_URL,
        json={
            "event_type": "subscription_started",
            "event_properties": {
                "profile_event_id": str(uuid.uuid4()),
                "customer_user_id": user_id,
                "vendor_product_id": "Week_6.99_Not_Trial",
                "transaction_id": 770002,
            },
        },
        headers=_ADAPTY_AUTH,
    )
    async with app.state.sessionmaker() as session:
        stored = await session.scalar(
            text("SELECT product_external_id FROM subscription_state WHERE user_id = :u"),
            {"u": user_id},
        )
    assert stored == "week_6.99_not_trial"


# --- Инвариант каталога ----------------------------------------------------


@pytest.mark.asyncio
async def test_case_colliding_products_are_rejected_by_db(app):
    """Два продукта, различающихся только регистром, БД принять не должна.

    Без этого инварианта регистронезависимый резолв стал бы неоднозначным и упал бы с
    MultipleResultsFound прямо на пути начисления монет.
    """
    import sqlalchemy.exc

    async with app.state.sessionmaker() as session:
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            async with session.begin():
                await session.execute(
                    text(
                        "INSERT INTO products (external_product_id, kind, title, grants) "
                        "VALUES ('100_TOKENS_9.99', 'coin_pack', 'dup', '{}'::jsonb)"
                    )
                )
