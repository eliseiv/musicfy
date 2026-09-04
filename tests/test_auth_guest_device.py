"""Идемпотентность гостевого входа по `deviceId`.

Клиент зовёт `POST /v1/auth/guest` при каждом холодном старте. До фикса второй вызов с тем
же `deviceId` падал на `uq_auth_identities_provider_subject` и возвращал 500 — приложение
после переустановки не могло войти вообще, а вместе с сессией терялись монеты и история
устройства (на них же завязан резолв пользователя в вебхуке Adapty, ADR-019).
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text

from tests.helpers import auth_headers

_URL = "/v1/auth/guest"


async def _guest(client, device_id: str | None = None) -> dict:
    payload = {} if device_id is None else {"deviceId": device_id}
    r = await client.post(_URL, json=payload)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_repeated_device_id_returns_same_user(client):
    """Основной регресс: повторный вход тем же устройством — тот же пользователь, не 500."""
    device_id = str(uuid.uuid4())
    first = await _guest(client, device_id)
    second = await _guest(client, device_id)

    assert second["userId"] == first["userId"]
    assert second["isGuest"] is True
    # Сессия новая (старый токен не переиспользуется), но пользователь тот же.
    assert second["token"] != first["token"]


@pytest.mark.asyncio
async def test_repeated_device_id_keeps_coins(client, app):
    """Баланс устройства переживает повторный вход — иначе теряются оплаченные монеты."""
    device_id = str(uuid.uuid4())
    first = await _guest(client, device_id)
    headers = {"Authorization": f"Bearer {first['token']}"}

    async with app.state.sessionmaker() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO coin_wallets (user_id, coins_available, coins_reserved) "
                    "VALUES (:u, 500, 0)"
                ),
                {"u": first["userId"]},
            )

    second = await _guest(client, device_id)
    headers = {"Authorization": f"Bearer {second['token']}"}
    balance = await client.get("/v1/billing/balance", headers=headers)
    assert balance.json()["coinsAvailable"] == 500


@pytest.mark.asyncio
async def test_old_session_stays_valid_after_relogin(client):
    """Повторный вход не отзывает прежнюю сессию: устройства не выбивают друг друга."""
    device_id = str(uuid.uuid4())
    first = await _guest(client, device_id)
    await _guest(client, device_id)

    me = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {first['token']}"})
    assert me.status_code == 200
    assert me.json()["userId"] == first["userId"]


@pytest.mark.asyncio
async def test_different_device_ids_are_different_users(client):
    """Разные устройства не должны схлопываться в одного пользователя."""
    a = await _guest(client, str(uuid.uuid4()))
    b = await _guest(client, str(uuid.uuid4()))
    assert a["userId"] != b["userId"]


@pytest.mark.asyncio
async def test_without_device_id_always_new_user(client):
    """Без deviceId идентифицировать устройство нечем — каждый вызов даёт нового гостя."""
    a = await _guest(client)
    b = await _guest(client)
    assert a["userId"] != b["userId"]


@pytest.mark.asyncio
async def test_case_differing_device_ids_are_distinct(client, app):
    """UPPER/lower deviceId — разные identity.

    Фиксирует фактическое поведение: уникальность `(provider, subject)` регистрозависима.
    Резолв пользователя в вебхуке Adapty, наоборот, сравнивает через `lower()` — поэтому
    клиент обязан слать deviceId в СТАБИЛЬНОМ регистре, иначе на одно устройство заведётся
    две учётки, а вебхук выберет из них одну детерминированно (ADR-019).
    """
    device_id = str(uuid.uuid4())
    lower = await _guest(client, device_id.lower())
    upper = await _guest(client, device_id.upper())
    assert lower["userId"] != upper["userId"]


@pytest.mark.asyncio
async def test_concurrent_cold_starts_do_not_500(client):
    """Гонка двух холодных стартов одного устройства: оба получают сессию, а не 500."""
    device_id = str(uuid.uuid4())
    r1, r2 = await asyncio.gather(
        client.post(_URL, json={"deviceId": device_id}),
        client.post(_URL, json={"deviceId": device_id}),
    )
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    # Обе сессии валидны и ведут на одного и того же пользователя.
    assert r1.json()["userId"] == r2.json()["userId"]


@pytest.mark.asyncio
async def test_promoted_apple_account_not_handed_out_by_device_id(client, app):
    """Знание deviceId не должно заменять Sign in with Apple.

    Если устройство принадлежит уже промоутнутому аккаунту, гостевой вход обязан выдать
    НОВОГО гостя, а не сессию постоянного аккаунта.
    """
    device_id = str(uuid.uuid4())
    guest = await _guest(client, device_id)

    # Промоутим пользователя устройства в постоянный аккаунт напрямую в БД —
    # так тест не зависит от инфраструктуры проверки Apple-токена.
    async with app.state.sessionmaker() as session:
        async with session.begin():
            await session.execute(
                text("UPDATE users SET is_guest = false WHERE id = :u"),
                {"u": guest["userId"]},
            )

    again = await _guest(client, device_id)
    assert again["userId"] != guest["userId"]
    assert again["isGuest"] is True


@pytest.mark.asyncio
async def test_guest_identity_row_is_reused_not_duplicated(client, app):
    """На устройство остаётся ровно одна guest-identity — иначе резолв станет неоднозначным."""
    device_id = str(uuid.uuid4())
    await _guest(client, device_id)
    await _guest(client, device_id)
    await _guest(client, device_id)

    async with app.state.sessionmaker() as session:
        count = await session.scalar(
            text(
                "SELECT count(*) FROM auth_identities "
                "WHERE provider = 'guest' AND subject = :s"
            ),
            {"s": device_id},
        )
    assert count == 1


@pytest.mark.asyncio
async def test_existing_flow_without_device_id_still_works(client):
    """Регресс-страховка: обычный гостевой вход и /me не сломаны."""
    headers = await auth_headers(client)
    me = await client.get("/v1/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["isGuest"] is True
