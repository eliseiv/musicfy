"""ADR-013 D2 — интеграционные тесты дедупа покупок через API (реальная БД).

Проверяется, что грант монет происходит ТОЛЬКО при реально вставленной строке `purchases`,
что чужой боевой чек нельзя погасить (replay), что слои `purchases` и `credit_ledger` не
расходятся, и что Xcode-повтор после сброса начисляет монеты снова (кейс разработчика).

Окружение транзакции берётся из claim `environment` (в тестах verify_signature=false,
см. conftest) — синтетические токены крафтятся локально с нужным окружением/датой.
"""
from __future__ import annotations

import jwt
import pytest
from sqlalchemy import text

from tests.helpers import auth_user

SMALL = "com.musicfy.coins.small"  # 100 монет


def _signed(
    product_id: str,
    transaction_id: str,
    *,
    environment: str,
    purchase_date_ms: int | None = None,
) -> str:
    """Синтетический StoreKit-токен с явным окружением/датой (verify_signature=false)."""
    claims: dict = {
        "transactionId": transaction_id,
        "originalTransactionId": transaction_id,
        "productId": product_id,
        "environment": environment,
    }
    if purchase_date_ms is not None:
        claims["purchaseDate"] = purchase_date_ms
    return jwt.encode(claims, "test-key", algorithm="HS256")


async def _balance(client, headers) -> dict:
    return (await client.get("/v1/billing/balance", headers=headers)).json()


async def _verify(client, headers, signed: str):
    return await client.post(
        "/v1/billing/purchases/verify", json={"signedTransaction": signed}, headers=headers
    )


# --------------------------------------------------------------------------
# D2: грант только при newly (свой повтор не начисляет второй раз)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_own_production_repeat_grants_once(client):
    """Свой повтор Production-чека → {status:ok, deduplicated:true}, баланс не растёт."""
    _, headers = await auth_user(client)
    signed = _signed(SMALL, "prod-own-1", environment="Production")

    r1 = await _verify(client, headers, signed)
    assert r1.status_code == 200
    assert r1.json() == {"status": "ok", "deduplicated": False, "reason": None}
    assert await _balance(client, headers) == {"coinsAvailable": 100, "coinsReserved": 0}

    r2 = await _verify(client, headers, signed)
    assert r2.json()["status"] == "ok"
    assert r2.json()["deduplicated"] is True
    # монеты начислены ровно один раз
    assert await _balance(client, headers) == {"coinsAvailable": 100, "coinsReserved": 0}


# --------------------------------------------------------------------------
# D2: replay чужого чека — КЛЮЧЕВОЙ security-тест
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_of_foreign_receipt_is_rejected(client):
    """Другой user, тот же `Production:{tx}` → rejected/transaction_already_claimed, баланс 0.

    Нельзя погасить чужой боевой чек: глобальный Production-ключ уже принадлежит user A.
    """
    _, headers_a = await auth_user(client)
    _, headers_b = await auth_user(client)
    signed = _signed(SMALL, "prod-shared-tx", environment="Production")

    # A применяет чек — получает 100
    await _verify(client, headers_a, signed)
    assert await _balance(client, headers_a) == {"coinsAvailable": 100, "coinsReserved": 0}

    # B пытается применить ТОТ ЖЕ чек под своим аккаунтом
    rb = await _verify(client, headers_b, signed)
    assert rb.status_code == 200
    body = rb.json()
    assert body["status"] == "rejected"
    assert body["reason"] == "transaction_already_claimed"
    assert body["deduplicated"] is False

    # баланс B не изменился, баланс A не пострадал
    assert await _balance(client, headers_b) == {"coinsAvailable": 0, "coinsReserved": 0}
    assert await _balance(client, headers_a) == {"coinsAvailable": 100, "coinsReserved": 0}


# --------------------------------------------------------------------------
# D2: слои не расходятся — ledger `purchase:{dedup_key}` == purchases.dedup_key
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ledger_key_matches_purchase_dedup_key(client, app):
    """Единый источник истины: idempotency_key ленджера = `purchase:` + purchases.dedup_key."""
    user_id, headers = await auth_user(client)
    await _verify(client, headers, _signed(SMALL, "layers-tx", environment="Production"))

    async with app.state.sessionmaker() as s:
        dedup_key = (
            await s.execute(
                text("SELECT dedup_key FROM purchases WHERE user_id = CAST(:u AS uuid)"),
                {"u": user_id},
            )
        ).scalar_one()
        ledger_key = (
            await s.execute(
                text(
                    "SELECT idempotency_key FROM credit_ledger "
                    "WHERE user_id = CAST(:u AS uuid) AND ref_type = 'transaction'"
                ),
                {"u": user_id},
            )
        ).scalar_one()

    assert dedup_key == "Production:layers-tx"
    assert ledger_key == f"purchase:{dedup_key}"


# --------------------------------------------------------------------------
# Кейс Максима через API: Xcode-повтор после сброса начисляет снова
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_xcode_repeat_after_reset_credits_again(client):
    """Тот же tx='0' с новым purchaseDate → монеты начисляются; тот же payload → дедуп."""
    _, headers = await auth_user(client)

    # первая Xcode-покупка (счётчик '0')
    await _verify(client, headers, _signed(SMALL, "0", environment="Xcode", purchase_date_ms=1000))
    assert await _balance(client, headers) == {"coinsAvailable": 100, "coinsReserved": 0}

    # после *Delete All Transactions*: тот же '0', НОВЫЙ purchaseDate → новый ключ → +100
    await _verify(client, headers, _signed(SMALL, "0", environment="Xcode", purchase_date_ms=2000))
    assert await _balance(client, headers) == {"coinsAvailable": 200, "coinsReserved": 0}

    # тот же payload повторно (тот же purchaseDate) → дедуп, баланс не растёт
    same = _signed(SMALL, "0", environment="Xcode", purchase_date_ms=2000)
    r = await _verify(client, headers, same)
    assert r.json()["deduplicated"] is True
    assert await _balance(client, headers) == {"coinsAvailable": 200, "coinsReserved": 0}
