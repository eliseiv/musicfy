"""Вебхук подписок Adapty (ADR-019): авторизация, матрица исходов, идемпотентность.

Ключевой инвариант, ради которого написана бо́льшая часть файла: одна реальная покупка
порождает НЕСКОЛЬКО событий Adapty с разными `profile_event_id`, но одним `transaction_id`,
и монеты за неё должны начислиться РОВНО ОДИН раз.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from tests.helpers import auth_user

_URL = "/v1/billing/adapty/webhook"
_SECRET = "test-adapty-secret"  # conftest: ADAPTY_WEBHOOK_SECRET
_AUTH = {"Authorization": f"Bearer {_SECRET}"}

# Реальный transaction_id из боевого payload — приходит голым int, без кавычек.
_TXN = 410003298316682
_WEEK = "week_6.99_not_trial"
_EXPIRES = "2026-09-07T09:05:46Z"


def _event(
    *,
    event_type: str = "subscription_started",
    profile_event_id: str | None = None,
    customer_user_id: str | None = None,
    product: str = _WEEK,
    txn: object = _TXN,
    is_active: object = None,
    access_level_id: object = None,
    will_renew: object = None,
    expires_at: str | None = _EXPIRES,
) -> dict:
    """Собирает тело в РЕАЛЬНОМ wire-формате Adapty: profile_event_id + event_properties."""
    props: dict = {
        "profile_event_id": profile_event_id or str(uuid.uuid4()),
        "vendor_product_id": product,
        "transaction_id": txn,
        "original_transaction_id": txn,
        "profile_id": str(uuid.uuid4()),
        "store": "app_store",
    }
    if customer_user_id is not None:
        props["customer_user_id"] = customer_user_id
    if expires_at is not None:
        props["subscription_expires_at"] = expires_at
    if is_active is not None:
        props["is_active"] = is_active
    if access_level_id is not None:
        props["access_level_id"] = access_level_id
    if will_renew is not None:
        props["will_renew"] = will_renew
    return {"event_type": event_type, "event_properties": props}


async def _balance(client, headers) -> int:
    r = await client.get("/v1/billing/balance", headers=headers)
    return r.json()["coinsAvailable"]


async def _subscription(app, user_id: str) -> dict | None:
    async with app.state.sessionmaker() as session:
        row = (
            await session.execute(
                text(
                    "SELECT status::text, provider::text, product_external_id, will_renew "
                    "FROM subscription_state WHERE user_id = :u"
                ),
                {"u": user_id},
            )
        ).first()
    if row is None:
        return None
    return {
        "status": row[0],
        "provider": row[1],
        "product": row[2],
        "will_renew": row[3],
    }


async def _ledger_count(app, user_id: str, key: str) -> int:
    async with app.state.sessionmaker() as session:
        return await session.scalar(
            text(
                "SELECT count(*) FROM credit_ledger "
                "WHERE user_id = :u AND idempotency_key = :k"
            ),
            {"u": user_id, "k": key},
        )


# --- Авторизация -----------------------------------------------------------


@pytest.mark.asyncio
async def test_no_bearer_returns_401(client):
    r = await client.post(_URL, json=_event())
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_wrong_bearer_returns_401(client):
    r = await client.post(
        _URL, json=_event(), headers={"Authorization": "Bearer nope"}
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_valid_bearer_reaches_body_handling(client):
    """Верный секрет → тело обрабатывается: пустой пинг Adapty получает 200, не 401/422."""
    r = await client.post(_URL, content=b"", headers=_AUTH)
    assert r.status_code == 200
    assert r.json() == {"result": "ignored", "reason": "empty_body"}


# --- Матрица исходов (всё после авторизации — 200) --------------------------


@pytest.mark.asyncio
async def test_non_json_body_is_ignored_not_422(client):
    """422 заставил бы Adapty ретраить это тело бесконечно."""
    r = await client.post(_URL, content=b"not json at all", headers=_AUTH)
    assert r.status_code == 200
    assert r.json()["reason"] == "invalid_json"


@pytest.mark.asyncio
async def test_json_array_is_not_an_object(client):
    r = await client.post(_URL, json=[1, 2, 3], headers=_AUTH)
    assert r.status_code == 200
    assert r.json()["reason"] == "not_an_object"


@pytest.mark.asyncio
async def test_missing_event_id(client):
    r = await client.post(
        _URL, json={"event_type": "subscription_started"}, headers=_AUTH
    )
    assert r.status_code == 200
    assert r.json()["reason"] == "missing_event_id"


@pytest.mark.asyncio
async def test_missing_customer_user_id_echoes_event_type(client):
    """Реальный payload без `Adapty.identify` несёт только profile_id — ожидаемая причина."""
    r = await client.post(_URL, json=_event(customer_user_id=None), headers=_AUTH)
    assert r.status_code == 200
    assert r.json()["reason"] == "missing_customer_user_id"


@pytest.mark.asyncio
async def test_non_uuid_customer_user_id(client):
    r = await client.post(_URL, json=_event(customer_user_id="not-a-uuid"), headers=_AUTH)
    assert r.status_code == 200
    assert r.json()["reason"] == "missing_customer_user_id"


@pytest.mark.asyncio
async def test_unknown_user_is_ignored(client):
    r = await client.post(
        _URL, json=_event(customer_user_id=str(uuid.uuid4())), headers=_AUTH
    )
    assert r.status_code == 200
    assert r.json()["reason"] == "user_not_found"


@pytest.mark.asyncio
async def test_unknown_event_type_echoes_type_without_reason(client):
    user_id, _ = await auth_user(client)
    r = await client.post(
        _URL,
        json=_event(event_type="non_subscription_purchase", customer_user_id=user_id),
        headers=_AUTH,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["result"] == "ignored"
    assert body["eventType"] == "non_subscription_purchase"
    assert "reason" not in body  # response_model_exclude_none


@pytest.mark.asyncio
async def test_webhook_never_creates_users(client, app):
    """Вебхук не источник регистрации: неизвестный id не должен провижинить пользователя."""
    unknown = str(uuid.uuid4())
    await client.post(_URL, json=_event(customer_user_id=unknown), headers=_AUTH)
    async with app.state.sessionmaker() as session:
        exists = await session.scalar(
            text("SELECT count(*) FROM users WHERE id = :u"), {"u": unknown}
        )
    assert exists == 0


# --- Начисление ------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscription_started_grants_catalog_amount(client, app):
    """Размер гранта берётся из каталога products, а не из payload и не из имени продукта."""
    user_id, headers = await auth_user(client)
    before = await _balance(client, headers)

    r = await client.post(_URL, json=_event(customer_user_id=user_id), headers=_AUTH)
    assert r.status_code == 200
    assert r.json() == {"result": "applied"}

    assert await _balance(client, headers) - before == 700  # week_6.99_not_trial
    sub = await _subscription(app, user_id)
    assert sub == {
        "status": "active",
        "provider": "adapty",
        "product": _WEEK,
        "will_renew": None,
    }


@pytest.mark.asyncio
async def test_grant_uses_adapty_txn_idempotency_key(client, app):
    """Ключ гранта — `adapty-txn:{transaction_id}`, отдельный namespace от StoreKit-пути."""
    user_id, _ = await auth_user(client)
    await client.post(_URL, json=_event(customer_user_id=user_id), headers=_AUTH)
    assert await _ledger_count(app, user_id, f"adapty-txn:{_TXN}") == 1


@pytest.mark.asyncio
async def test_unknown_product_falls_back_to_configured_grant(client, app):
    """Продукт ещё не засеян → платящий пользователь всё равно получает монеты."""
    user_id, headers = await auth_user(client)
    before = await _balance(client, headers)
    r = await client.post(
        _URL,
        json=_event(customer_user_id=user_id, product="not_in_catalog_yet"),
        headers=_AUTH,
    )
    assert r.json()["result"] == "applied"
    assert await _balance(client, headers) - before == 1000  # ADAPTY_SUBSCRIPTION_COINS_GRANT


@pytest.mark.asyncio
async def test_yearly_product_grants_its_own_tier(client, headers=None):
    user_id, headers = await auth_user(client)
    before = await _balance(client, headers)
    await client.post(
        _URL,
        json=_event(customer_user_id=user_id, product="yearly_49.99_not_trial"),
        headers=_AUTH,
    )
    assert await _balance(client, headers) - before == 5000


# --- Идемпотентность: два независимых слоя ---------------------------------


@pytest.mark.asyncio
async def test_real_purchase_three_events_grants_once(client, app):
    """Инвариант ADR-019: одна покупка = три события с одним txn = ОДИН грант.

    Именно этот сценарий ломается, если дедупить только по event_id.
    """
    user_id, headers = await auth_user(client)
    before = await _balance(client, headers)

    for payload in (
        _event(event_type="trial_started", customer_user_id=user_id),
        _event(
            event_type="access_level_updated",
            customer_user_id=user_id,
            is_active=True,
            access_level_id="premium",
        ),
        _event(
            event_type="trial_renewal_cancelled",
            customer_user_id=user_id,
            will_renew=False,
        ),
    ):
        r = await client.post(_URL, json=payload, headers=_AUTH)
        assert r.json()["result"] == "applied"

    assert await _balance(client, headers) - before == 700
    assert await _ledger_count(app, user_id, f"adapty-txn:{_TXN}") == 1


@pytest.mark.asyncio
async def test_replayed_event_id_is_duplicate_without_side_effects(client, app):
    user_id, headers = await auth_user(client)
    payload = _event(customer_user_id=user_id)

    first = await client.post(_URL, json=payload, headers=_AUTH)
    assert first.json()["result"] == "applied"
    after_first = await _balance(client, headers)

    second = await client.post(_URL, json=payload, headers=_AUTH)
    assert second.json() == {"result": "duplicate"}
    assert await _balance(client, headers) == after_first


@pytest.mark.asyncio
async def test_renewal_with_new_transaction_grants_again(client, headers=None):
    """Продление — новый transaction_id → новый период → новые монеты."""
    user_id, headers = await auth_user(client)
    before = await _balance(client, headers)

    await client.post(_URL, json=_event(customer_user_id=user_id), headers=_AUTH)
    await client.post(
        _URL,
        json=_event(
            event_type="subscription_renewed", customer_user_id=user_id, txn=_TXN + 1
        ),
        headers=_AUTH,
    )
    assert await _balance(client, headers) - before == 1400


# --- Семантика событий -----------------------------------------------------


@pytest.mark.asyncio
async def test_renewal_cancelled_keeps_access_and_coins(client, app):
    """Отмена автопродления ≠ отзыв доступа: статус и монеты не трогаем."""
    user_id, headers = await auth_user(client)
    await client.post(_URL, json=_event(customer_user_id=user_id), headers=_AUTH)
    after_grant = await _balance(client, headers)

    r = await client.post(
        _URL,
        json=_event(
            event_type="subscription_renewal_cancelled",
            customer_user_id=user_id,
            will_renew=False,
        ),
        headers=_AUTH,
    )
    assert r.json()["result"] == "applied"
    assert await _balance(client, headers) == after_grant
    assert (await _subscription(app, user_id))["status"] == "active"


@pytest.mark.asyncio
async def test_expired_marks_expired_and_keeps_coins(client, app):
    user_id, headers = await auth_user(client)
    await client.post(_URL, json=_event(customer_user_id=user_id), headers=_AUTH)
    after_grant = await _balance(client, headers)

    await client.post(
        _URL,
        json=_event(event_type="subscription_expired", customer_user_id=user_id),
        headers=_AUTH,
    )
    assert (await _subscription(app, user_id))["status"] == "expired"
    assert await _balance(client, headers) == after_grant


@pytest.mark.asyncio
async def test_cancelled_marks_canceled(client, app):
    user_id, _ = await auth_user(client)
    await client.post(_URL, json=_event(customer_user_id=user_id), headers=_AUTH)
    await client.post(
        _URL,
        json=_event(event_type="subscription_cancelled", customer_user_id=user_id),
        headers=_AUTH,
    )
    assert (await _subscription(app, user_id))["status"] == "canceled"


@pytest.mark.asyncio
async def test_access_level_updated_inactive_expires(client, app):
    user_id, headers = await auth_user(client)
    await client.post(_URL, json=_event(customer_user_id=user_id), headers=_AUTH)
    after_grant = await _balance(client, headers)

    await client.post(
        _URL,
        json=_event(
            event_type="access_level_updated",
            customer_user_id=user_id,
            is_active=False,
        ),
        headers=_AUTH,
    )
    assert (await _subscription(app, user_id))["status"] == "canceled"
    assert await _balance(client, headers) == after_grant


@pytest.mark.asyncio
async def test_access_level_updated_active_non_premium_is_noop(client, app):
    """active, но не premium → ничего не делаем: доступ не выдаём и не отзываем."""
    user_id, headers = await auth_user(client)
    before = await _balance(client, headers)
    r = await client.post(
        _URL,
        json=_event(
            event_type="access_level_updated",
            customer_user_id=user_id,
            is_active=True,
            access_level_id="basic",
        ),
        headers=_AUTH,
    )
    assert r.json()["result"] == "applied"
    assert await _balance(client, headers) == before
    assert await _subscription(app, user_id) is None


@pytest.mark.asyncio
async def test_is_active_integer_one_does_not_grant_premium(client, app):
    """Строгая проверка bool: `1` — не `true`, доступ по нему не выдаётся."""
    user_id, headers = await auth_user(client)
    before = await _balance(client, headers)
    await client.post(
        _URL,
        json=_event(
            event_type="access_level_updated",
            customer_user_id=user_id,
            is_active=1,
            access_level_id="premium",
        ),
        headers=_AUTH,
    )
    assert await _balance(client, headers) == before


@pytest.mark.asyncio
async def test_unparseable_expires_at_still_applied(client, app):
    user_id, _ = await auth_user(client)
    r = await client.post(
        _URL,
        json=_event(customer_user_id=user_id, expires_at="не дата"),
        headers=_AUTH,
    )
    assert r.json()["result"] == "applied"
    assert (await _subscription(app, user_id))["status"] == "active"
