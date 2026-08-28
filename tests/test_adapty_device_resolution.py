"""Резолв пользователя вебхука Adapty через device-id (ADR-019).

Adapty кладёт в `customer_user_id` то, что iOS передал в `Adapty.identify(...)` — на практике
deviceId, а не наш userId. Без второй ступени резолва реальные оплаты падали бы в
`user_not_found`, и монеты не доезжали бы до платящего пользователя.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from tests.helpers import auth_user
from tests.test_adapty_webhook import _AUTH, _URL, _balance, _event


async def _guest_with_device(client, device_id: str) -> tuple[str, dict]:
    """Гостевой вход с явным device_id → он ложится в auth_identities.subject."""
    r = (await client.post("/v1/auth/guest", json={"deviceId": device_id})).json()
    return r["userId"], {"Authorization": f"Bearer {r['token']}"}


@pytest.mark.asyncio
async def test_device_id_resolves_and_credits_linked_user(client, app):
    device_id = str(uuid.uuid4())
    user_id, headers = await _guest_with_device(client, device_id)
    before = await _balance(client, headers)

    r = await client.post(
        _URL, json=_event(customer_user_id=device_id), headers=_AUTH
    )
    assert r.json()["result"] == "applied"
    assert await _balance(client, headers) - before == 700

    # Событие и монеты записаны на НАШЕГО пользователя, а не на присланный deviceId.
    async with app.state.sessionmaker() as session:
        owner = await session.scalar(
            text("SELECT user_id::text FROM adapty_webhook_events LIMIT 1")
        )
    assert owner == user_id


@pytest.mark.asyncio
async def test_uppercase_device_id_resolves(client):
    """iOS отдаёт identifierForVendor в UPPERCASE, а str(uuid) — lowercase.

    Точное сравнение потеряло бы ровно тех пользователей, ради которых ветка и написана.
    """
    device_id = str(uuid.uuid4()).upper()
    _, headers = await _guest_with_device(client, device_id)
    before = await _balance(client, headers)

    # Adapty пришлёт идентификатор нормализованным (lowercase) — как его отдаёт SDK.
    r = await client.post(
        _URL, json=_event(customer_user_id=device_id.lower()), headers=_AUTH
    )
    assert r.json()["result"] == "applied"
    assert await _balance(client, headers) - before == 700


@pytest.mark.asyncio
async def test_direct_user_id_still_wins(client):
    """Обратная совместимость: если пришёл наш userId — резолвим по users, не по устройству."""
    user_id, headers = await auth_user(client)
    before = await _balance(client, headers)
    r = await client.post(_URL, json=_event(customer_user_id=user_id), headers=_AUTH)
    assert r.json()["result"] == "applied"
    assert await _balance(client, headers) - before == 700


@pytest.mark.asyncio
async def test_unrelated_uuid_is_user_not_found(client):
    """Незнакомый идентификатор не должен зачислить монеты «кому-нибудь»."""
    _, headers = await _guest_with_device(client, str(uuid.uuid4()))
    before = await _balance(client, headers)
    r = await client.post(
        _URL, json=_event(customer_user_id=str(uuid.uuid4())), headers=_AUTH
    )
    assert r.json()["reason"] == "user_not_found"
    assert await _balance(client, headers) == before
