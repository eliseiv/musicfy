"""ADR-017 — перенос подписки при переустановке (guest-аккаунты).

Сценарий реджекта Apple (Guideline 2.1(a), июль 2026): переустановка приложения создаёт
нового guest-пользователя, StoreKit предъявляет чек активной подписки, а verify отвечал
`rejected/transaction_already_claimed` — подписку было невозможно ни купить, ни восстановить.

После ADR-017 подписочная цепочка следует за Apple ID: чек чужого (брошенного) аккаунта
переносит entitlement на текущего пользователя без переначисления монет. Replay коин-паков
остаётся отклонённым (ADR-013), Xcode-окружение в переносе не участвует.
"""
from __future__ import annotations

import time

import jwt
import pytest
from sqlalchemy import text

from tests.helpers import auth_user

WEEKLY = "week_6.99_not_trial"  # подписка, грант 100 монет (сид миграции 0017)
SMALL = "100_tokens_9.99"  # коин-пак, 100 монет


def _signed_sub(
    transaction_id: str,
    *,
    environment: str = "Sandbox",
    original_transaction_id: str | None = None,
    product_id: str = WEEKLY,
) -> str:
    """Синтетический подписочный JWS (verify_signature=false, см. conftest)."""
    claims = {
        "transactionId": transaction_id,
        "originalTransactionId": original_transaction_id or transaction_id,
        "productId": product_id,
        "environment": environment,
        "type": "Auto-Renewable Subscription",
        "expiresDate": int((time.time() + 7 * 86400) * 1000),
    }
    return jwt.encode(claims, "test-key", algorithm="HS256")


async def _verify(client, headers, signed: str):
    return await client.post(
        "/v1/billing/purchases/verify", json={"signedTransaction": signed}, headers=headers
    )


async def _balance(client, headers) -> dict:
    return (await client.get("/v1/billing/balance", headers=headers)).json()


async def _subscription_row(app, user_id: str) -> dict | None:
    async with app.state.sessionmaker() as s:
        row = (
            await s.execute(
                text(
                    "SELECT status, product_external_id, original_transaction_id "
                    "FROM subscription_state WHERE user_id = CAST(:u AS uuid)"
                ),
                {"u": user_id},
            )
        ).mappings().first()
        return dict(row) if row else None


async def _purchase_owners(app, original_transaction_id: str) -> set[str]:
    async with app.state.sessionmaker() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT DISTINCT user_id::text FROM purchases "
                    "WHERE original_transaction_id = :otid"
                ),
                {"otid": original_transaction_id},
            )
        ).scalars().all()
        return set(rows)


# --------------------------------------------------------------------------
# Кейс Apple-ревью: тот же чек с нового guest-аккаунта → перенос, не reject
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reinstall_same_receipt_transfers_subscription(client, app):
    """B предъявляет чек подписки, погашенный A → ok/subscription_transferred.

    A (старый guest) теряет active-статус, B получает подписку. Монеты не
    переначисляются: reinstall не должен быть фармом грантов.
    """
    user_a, headers_a = await auth_user(client)
    user_b, headers_b = await auth_user(client)
    signed = _signed_sub("sub-tx-1")

    ra = await _verify(client, headers_a, signed)
    assert ra.json() == {"status": "ok", "deduplicated": False, "reason": None}
    assert await _balance(client, headers_a) == {"coinsAvailable": 100, "coinsReserved": 0}
    assert (await _subscription_row(app, user_a))["status"] == "active"

    # переустановка: новый guest предъявляет ТОТ ЖЕ чек
    rb = await _verify(client, headers_b, signed)
    assert rb.status_code == 200
    assert rb.json() == {
        "status": "ok",
        "deduplicated": True,
        "reason": "subscription_transferred",
    }

    sub_b = await _subscription_row(app, user_b)
    assert sub_b["status"] == "active"
    assert sub_b["product_external_id"] == WEEKLY
    assert (await _subscription_row(app, user_a))["status"] == "expired"
    # цепочка purchases переехала к B (webhooks найдут актуального владельца)
    assert await _purchase_owners(app, "sub-tx-1") == {user_b}
    # монеты начислены ровно один раз — прежнему владельцу
    assert await _balance(client, headers_b) == {"coinsAvailable": 0, "coinsReserved": 0}
    assert await _balance(client, headers_a) == {"coinsAvailable": 100, "coinsReserved": 0}


@pytest.mark.asyncio
async def test_restore_after_reinstall_transfers_subscription(client, app):
    """/restore после переустановки — тот же перенос, что и verify (общий путь)."""
    _, headers_a = await auth_user(client)
    user_b, headers_b = await auth_user(client)
    signed = _signed_sub("sub-tx-restore")

    await _verify(client, headers_a, signed)

    r = await client.post(
        "/v1/billing/restore", json={"signedTransactions": [signed]}, headers=headers_b
    )
    assert r.status_code == 200
    assert r.json() == [
        {"status": "ok", "deduplicated": True, "reason": "subscription_transferred"}
    ]
    assert (await _subscription_row(app, user_b))["status"] == "active"


# --------------------------------------------------------------------------
# Resubscribe/renewal после переустановки: новый tx той же цепочки
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_transaction_same_chain_takes_over(client, app):
    """Новый tx с original_transaction_id цепочки A → монеты B, A гасится, история переезжает."""
    user_a, headers_a = await auth_user(client)
    user_b, headers_b = await auth_user(client)

    await _verify(client, headers_a, _signed_sub("chain-1"))
    rb = await _verify(
        client, headers_b, _signed_sub("chain-2", original_transaction_id="chain-1")
    )
    # новая транзакция: полноценное применение с грантом монет за новый период
    assert rb.json() == {"status": "ok", "deduplicated": False, "reason": None}
    assert await _balance(client, headers_b) == {"coinsAvailable": 100, "coinsReserved": 0}

    assert (await _subscription_row(app, user_b))["status"] == "active"
    assert (await _subscription_row(app, user_a))["status"] == "expired"
    assert await _purchase_owners(app, "chain-1") == {user_b}


# --------------------------------------------------------------------------
# Границы переноса: коин-паки и Xcode не участвуют
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coin_pack_replay_still_rejected(client):
    """Чужой чек коин-пака по-прежнему rejected (ADR-013): переносить нечего, монеты не дать."""
    _, headers_a = await auth_user(client)
    _, headers_b = await auth_user(client)
    signed = jwt.encode(
        {
            "transactionId": "pack-tx-1",
            "originalTransactionId": "pack-tx-1",
            "productId": SMALL,
            "environment": "Sandbox",
        },
        "test-key",
        algorithm="HS256",
    )

    await _verify(client, headers_a, signed)
    rb = await _verify(client, headers_b, signed)
    assert rb.json()["status"] == "rejected"
    assert rb.json()["reason"] == "transaction_already_claimed"
    assert await _balance(client, headers_b) == {"coinsAvailable": 0, "coinsReserved": 0}


@pytest.mark.asyncio
async def test_xcode_subscriptions_do_not_transfer(client, app):
    """Xcode-ID не уникальны: одинаковый tx у A и B → у каждого своя подписка, переноса нет."""
    user_a, headers_a = await auth_user(client)
    user_b, headers_b = await auth_user(client)

    ra = await _verify(client, headers_a, _signed_sub("2", environment="Xcode"))
    rb = await _verify(client, headers_b, _signed_sub("2", environment="Xcode"))
    # Xcode-дедуп per-user: обе покупки применяются независимо
    assert ra.json()["status"] == "ok"
    assert rb.json()["status"] == "ok"

    assert (await _subscription_row(app, user_a))["status"] == "active"
    assert (await _subscription_row(app, user_b))["status"] == "active"
    # покупки остались у своих владельцев
    assert await _purchase_owners(app, "2") == {user_a, user_b}
