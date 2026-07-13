"""ADR-013 D1 — юнит-тесты источника истины дедуп-ключа (`BillingService._dedup_key`).

Ключ — единственный дискриминатор области дедупа покупки. Здесь проверяется чистая логика
без БД: как окружение транзакции отображается в ключ и что Xcode-повтор после сброса
(*Delete All Transactions*, кейс разработчика) получает НОВЫЙ ключ за счёт `purchaseDate`.
"""
from __future__ import annotations

import uuid

import pytest

from app.domain.enums import StoreKitEnvironment
from app.domain.services.billing_service import BillingService

USER_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_B = uuid.UUID("22222222-2222-2222-2222-222222222222")


# --------------------------------------------------------------------------
# _environment: окружение из верифицированного payload; неизвестное → Production
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Production", StoreKitEnvironment.production),
        ("Sandbox", StoreKitEnvironment.sandbox),
        ("Xcode", StoreKitEnvironment.xcode),
    ],
)
def test_environment_maps_known_values(raw: str, expected: StoreKitEnvironment):
    assert BillingService._environment({"environment": raw}) is expected


@pytest.mark.parametrize("raw", ["", "Weird", "prod", None])
def test_environment_unknown_or_missing_falls_back_to_production(raw):
    """Неизвестное/отсутствующее окружение → Production = строжайший (глобальный) дедуп."""
    tx = {"transaction_id": "t1"} if raw is None else {"transaction_id": "t1", "environment": raw}
    assert BillingService._environment(tx) is StoreKitEnvironment.production


# --------------------------------------------------------------------------
# _dedup_key: область ключа по окружению
# --------------------------------------------------------------------------


def test_production_key_is_global_and_minimal():
    """Production: только `Production:{tx}` — ни user_id, ни даты (узкая боевая идентичность)."""
    tx = {"transaction_id": "999", "environment": "Production"}
    key = BillingService._dedup_key(USER_A, tx)
    assert key == "Production:999"
    # тот же боевой tx у другого пользователя даёт ТОТ ЖЕ ключ → replay-защита cross-user
    key_b = BillingService._dedup_key(USER_B, tx)
    assert key_b == key


def test_sandbox_key_is_global_and_namespaced():
    """Sandbox: `Sandbox:{tx}` — отдельный namespace, не сталкивается с Production."""
    key = BillingService._dedup_key(USER_A, {"transaction_id": "999", "environment": "Sandbox"})
    assert key == "Sandbox:999"
    prod = BillingService._dedup_key(USER_A, {"transaction_id": "999", "environment": "Production"})
    assert key != prod


def test_unknown_environment_uses_global_production_key():
    """Отсутствующее окружение → Production-ключ (глобальный), не per-user."""
    key = BillingService._dedup_key(USER_A, {"transaction_id": "999"})
    assert key == "Production:999"


def test_xcode_key_is_per_user_and_date():
    """Xcode: `Xcode:{user}:{tx}:{purchase_date_ms}` — на пользователя и момент покупки."""
    tx = {"transaction_id": "0", "environment": "Xcode", "purchase_date_ms": 1720000000000}
    key = BillingService._dedup_key(USER_A, tx)
    assert key == f"Xcode:{USER_A}:0:1720000000000"
    # другой пользователь с тем же тестовым ID → другой ключ (кросс-аккаунтная изоляция)
    key_b = BillingService._dedup_key(USER_B, tx)
    assert key_b == f"Xcode:{USER_B}:0:1720000000000"
    assert key_b != key


def test_xcode_missing_purchase_date_is_deterministic_zero():
    """Отсутствующий/нечисловой purchaseDate → 0 (ключ остаётся детерминированным)."""
    key = BillingService._dedup_key(USER_A, {"transaction_id": "0", "environment": "Xcode"})
    assert key == f"Xcode:{USER_A}:0:0"
    bad = BillingService._dedup_key(
        USER_A, {"transaction_id": "0", "environment": "Xcode", "purchase_date_ms": "not-a-number"}
    )
    assert bad == f"Xcode:{USER_A}:0:0"


# --------------------------------------------------------------------------
# Кейс Максима: Xcode-повтор после сброса → новый ключ; тот же payload → тот же ключ
# --------------------------------------------------------------------------


def test_xcode_repeat_after_reset_gets_new_key():
    """Тот же tx_id, НОВЫЙ purchaseDate (после *Delete All Transactions*) → новый ключ."""
    first = BillingService._dedup_key(
        USER_A, {"transaction_id": "0", "environment": "Xcode", "purchase_date_ms": 1000}
    )
    after_reset = BillingService._dedup_key(
        USER_A, {"transaction_id": "0", "environment": "Xcode", "purchase_date_ms": 2000}
    )
    assert first != after_reset


def test_xcode_same_payload_twice_is_same_key():
    """Тот же payload дважды (тот же purchaseDate) → тот же ключ → дедуп сохранён."""
    tx = {"transaction_id": "0", "environment": "Xcode", "purchase_date_ms": 1000}
    assert BillingService._dedup_key(USER_A, tx) == BillingService._dedup_key(USER_A, dict(tx))
