"""Тесты замены каталога продуктов на вербатим App Store Connect (ADR-015, миграция 0017).

Покрывает четыре инварианта ADR-015:
  1. Регресс-инвариант §3: покупка/продление ДЕАКТИВИРОВАННОГО старого продукта
     (`active=false`) резолвится (`get_by_external_id` без active-фильтра) и начисляет
     монеты — НЕ `unknown_product`. Защищает уже купивших и продления.
  2. Клиентский каталог §Решение/1: `GET /v1/billing/products` (`list_active`) отдаёт
     РОВНО 7 новых вербатим-продуктов с верными grants/period и без старых `com.musicfy.*`.
  3. Новый продукт начисляет верную сумму монет через реальный verify-flow.
  4. Идемпотентность/обратимость миграции 0017: round-trip `downgrade -1 && upgrade head`
     не плодит дублей и восстанавливает идентичное состояние (7 new active / 6 old inactive).

verify-flow реального пользователя: APP_ENV=test → verify_signature=false (conftest),
синтетические JWS-токены через make_signed_transaction (как в tests/test_billing.py).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import text

from app.config import get_settings
from app.db.session import build_engine
from tests.helpers import auth_headers, make_signed_transaction

# Репозиторий-рут (для запуска alembic из subprocess). parents[1] от tests/ .
REPO_ROOT = Path(__file__).resolve().parents[1]

# Вербатим-каталог ADR-015 §1: external_product_id -> (kind, coins, period_days).
# Источник истины теста; любое расхождение с App Store Connect воспроизводит unknown_product.
NEW_CATALOG: dict[str, tuple[str, int, int | None]] = {
    "100_tokens_9.99": ("coin_pack", 100, None),
    "250_tokens_19.99": ("coin_pack", 250, None),
    "500_tokens_34.99": ("coin_pack", 500, None),
    "1000_tokens_59.99": ("coin_pack", 1000, None),
    "2000_tokens_99.99": ("coin_pack", 2000, None),
    "week_6.99_not_trial": ("subscription", 100, 7),
    "yearly_49.99_not_trial": ("subscription", 1000, 365),
}

# Старый монетный каталог (0011), деактивируемый 0017. Grants сохраняются как были.
LEGACY_IDS = [
    "com.musicfy.coins.small",
    "com.musicfy.coins.medium",
    "com.musicfy.coins.large",
    "com.musicfy.coins.xl",
    "com.musicfy.sub.weekly",
    "com.musicfy.sub.yearly",
]


async def _balance(client, headers) -> dict:
    return (await client.get("/v1/billing/balance", headers=headers)).json()


# ==========================================================================
# Кейс 2. Клиентский каталог: ровно 7 новых вербатим-продуктов
# ==========================================================================


@pytest.mark.asyncio
async def test_products_endpoint_returns_exactly_new_verbatim_catalog(client):
    resp = await client.get("/v1/billing/products")
    assert resp.status_code == 200
    products = {p["productId"]: p for p in resp.json()}

    # Ровно 7 новых id — ни больше, ни меньше.
    assert set(products) == set(NEW_CATALOG)
    # Ни одного старого com.musicfy.* в клиентском каталоге (деактивированы).
    assert not any(pid.startswith("com.musicfy.") for pid in products)

    for pid, (kind, coins, period) in NEW_CATALOG.items():
        assert products[pid]["kind"] == kind, pid
        assert products[pid]["grants"] == {"coins": coins}, pid
        assert products[pid]["periodDays"] == period, pid


# ==========================================================================
# Кейс 3. Новый продукт начисляет верную сумму через реальный verify-flow
# ==========================================================================


@pytest.mark.asyncio
async def test_new_coin_pack_100_tokens_credits_wallet(client):
    headers = await auth_headers(client)
    signed = make_signed_transaction(
        product_id="100_tokens_9.99", transaction_id="tx-new-100tok"
    )
    r = await client.post(
        "/v1/billing/purchases/verify", json={"signedTransaction": signed}, headers=headers
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["deduplicated"] is False
    assert await _balance(client, headers) == {"coinsAvailable": 100, "coinsReserved": 0}
    ledger = (await client.get("/v1/billing/ledger", headers=headers)).json()
    assert any(e["kind"] == "credit_purchase" for e in ledger)


@pytest.mark.asyncio
async def test_new_yearly_subscription_credits_1000(client):
    headers = await auth_headers(client)
    signed = make_signed_transaction(
        product_id="yearly_49.99_not_trial",
        transaction_id="tx-new-yearly",
        expires_date_ms=int((time.time() + 365 * 86400) * 1000),
    )
    r = await client.post(
        "/v1/billing/purchases/verify", json={"signedTransaction": signed}, headers=headers
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["deduplicated"] is False
    assert await _balance(client, headers) == {"coinsAvailable": 1000, "coinsReserved": 0}
    ledger = (await client.get("/v1/billing/ledger", headers=headers)).json()
    assert any(e["kind"] == "credit_subscription_grant" for e in ledger)


# ==========================================================================
# Кейс 1. КРИТИЧНЫЙ регресс-инвариант (ADR-015 §3):
# деактивированный старый продукт РЕЗОЛВИТСЯ и начисляет — не unknown_product
# ==========================================================================


@pytest.mark.asyncio
async def test_legacy_inactive_subscription_still_resolves_and_credits(client):
    """Продление старой подписки после деактивации: get_by_external_id без active-фильтра.

    Если бы резолв фильтровал active=true, продление com.musicfy.sub.weekly у уже
    подписанного пользователя вернуло бы ignored/unknown_product и монеты не начислились.
    """
    headers = await auth_headers(client)
    signed = make_signed_transaction(
        product_id="com.musicfy.sub.weekly",
        transaction_id="tx-legacy-week",
        expires_date_ms=int((time.time() + 7 * 86400) * 1000),
    )
    r = await client.post(
        "/v1/billing/purchases/verify", json={"signedTransaction": signed}, headers=headers
    )
    assert r.status_code == 200
    body = r.json()
    # КРИТИЧНО: НЕ unknown_product — деактивированный продукт всё равно резолвится.
    assert body["status"] == "ok", body
    assert body.get("reason") != "unknown_product", body
    # Старый grant сохранён миграцией (0011: weekly = 150 монет).
    assert await _balance(client, headers) == {"coinsAvailable": 150, "coinsReserved": 0}
    ledger = (await client.get("/v1/billing/ledger", headers=headers)).json()
    assert any(e["kind"] == "credit_subscription_grant" for e in ledger)


@pytest.mark.asyncio
async def test_legacy_inactive_coin_pack_still_resolves_and_credits(client):
    """Аналогично для монетного пака: deactivated com.musicfy.coins.medium начисляет 550."""
    headers = await auth_headers(client)
    signed = make_signed_transaction(
        product_id="com.musicfy.coins.medium", transaction_id="tx-legacy-pack"
    )
    r = await client.post(
        "/v1/billing/purchases/verify", json={"signedTransaction": signed}, headers=headers
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok", body
    assert body.get("reason") != "unknown_product", body
    assert await _balance(client, headers) == {"coinsAvailable": 550, "coinsReserved": 0}
    ledger = (await client.get("/v1/billing/ledger", headers=headers)).json()
    assert any(e["kind"] == "credit_purchase" for e in ledger)


@pytest.mark.asyncio
async def test_truly_unknown_product_is_ignored(client):
    """Контраст к кейсу 1: несуществующий product_id → ignored/unknown_product, монет нет.

    Доказывает, что 'ok' для деактивированных продуктов — следствие резолва без active-фильтра,
    а не безусловного приёма любой транзакции.
    """
    headers = await auth_headers(client)
    signed = make_signed_transaction(
        product_id="com.musicfy.does.not.exist", transaction_id="tx-unknown"
    )
    r = await client.post(
        "/v1/billing/purchases/verify", json={"signedTransaction": signed}, headers=headers
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ignored"
    assert body["reason"] == "unknown_product"
    assert await _balance(client, headers) == {"coinsAvailable": 0, "coinsReserved": 0}


# ==========================================================================
# Кейс 4. Идемпотентность и обратимость миграции 0017 (round-trip)
# ==========================================================================


async def _catalog_state() -> dict[str, tuple[str, str, int | None, bool]]:
    """Снимок products: external_product_id -> (kind, grants_json, period_days, active)."""
    settings = get_settings()
    engine = build_engine(settings)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT external_product_id, kind::text, grants::text, "
                        "period_days, active FROM products"
                    )
                )
            ).all()
    finally:
        await engine.dispose()
    return {r[0]: (r[1], r[2], r[3], r[4]) for r in rows}


def _alembic(*args: str) -> None:
    """Кросс-платформенный запуск alembic (sys.executable -m alembic, без хардкода .exe)."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(REPO_ROOT),
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} failed (code {result.returncode}):\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )


@pytest.mark.asyncio
async def test_migration_0017_roundtrip_idempotent_and_reversible():
    """downgrade -1 && upgrade head восстанавливает идентичное состояние, без дублей.

    downgrade НЕ удаляет строки (FK-safe): деактивирует новые, реактивирует старые.
    Повторный upgrade идёт по ветке ON CONFLICT DO UPDATE (строки уже существуют),
    поэтому round-trip обязан быть точным no-op по составу каталога.
    """
    state_head = await _catalog_state()
    try:
        # Исходный инвариант head (0017): 7 новых active, 6 старых inactive.
        new_active = {
            k for k, v in state_head.items() if v[3] and not k.startswith("com.musicfy.")
        }
        old_inactive = {
            k for k, v in state_head.items() if not v[3] and k.startswith("com.musicfy.")
        }
        assert new_active == set(NEW_CATALOG), state_head
        assert old_inactive == set(LEGACY_IDS), state_head
        assert len(state_head) == 13

        # downgrade -1 → 0016: реактивация 6 старых, деактивация 7 новых, БЕЗ удаления строк.
        _alembic("downgrade", "-1")
        state_down = await _catalog_state()
        assert set(state_down) == set(state_head), "downgrade не должен удалять строки (FK-safe)"
        for pid in LEGACY_IDS:
            assert state_down[pid][3] is True, f"{pid} должен реактивироваться на downgrade"
        for pid in NEW_CATALOG:
            assert state_down[pid][3] is False, f"{pid} должен деактивироваться на downgrade"

        # upgrade head → 0017: upsert по ON CONFLICT DO UPDATE (строки уже есть).
        _alembic("upgrade", "head")
        state_up = await _catalog_state()
        # Round-trip идемпотентен и обратим: состояние идентично исходному, без дублей.
        assert state_up == state_head
        assert len(state_up) == 13

        # Второй round-trip: повторный прогон upsert по-прежнему даёт идентичное состояние.
        _alembic("downgrade", "-1")
        _alembic("upgrade", "head")
        state_up2 = await _catalog_state()
        assert state_up2 == state_head
        assert len(state_up2) == 13
    finally:
        # Гарантируем возврат БД на head даже при падении assert выше.
        _alembic("upgrade", "head")
