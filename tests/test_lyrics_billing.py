from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from tests.helpers import auth_user

# ADR-010 — биллинг синхронного POST /v1/lyrics: атомарный charge до fal + saga-refund.
# Цена lyrics в тест-БД по умолчанию ОТСУТСТВУЕТ (миграция 0009 сеет только
# song/cover/video; conftest.clean_db пересеивает канонические цены перед каждым
# тестом и truncate'ит generation_prices). Поэтому платные кейсы задают цену lyrics
# через admin PATCH внутри теста — изоляция гарантируется autouse clean_db.

ADMIN = {"Authorization": "Bearer test-admin-key"}
LYRICS_PRICE = 10


async def _set_lyrics_price(client, price: int = LYRICS_PRICE) -> None:
    resp = await client.patch(
        "/v1/admin/pricing/lyrics", json={"priceCoins": price}, headers=ADMIN
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["priceCoins"] == price


async def _grant(client, user_id: str, coins: int) -> None:
    resp = await client.post(
        f"/v1/admin/users/{user_id}/credits", json={"coins": coins}, headers=ADMIN
    )
    assert resp.status_code == 200, resp.text


async def _balance(client, headers) -> dict:
    return (await client.get("/v1/billing/balance", headers=headers)).json()


async def _ledger(client, headers) -> list[dict]:
    return (await client.get("/v1/billing/ledger", headers=headers)).json()


def _spy_generate_lyrics(app):
    """Оборачивает fal.generate_lyrics счётчиком вызовов (app function-scoped → чистый fal)."""
    fal = app.state.lyrics_service._fal
    calls: list[dict] = []
    original = fal.generate_lyrics

    async def _spy(**kwargs):
        calls.append(kwargs)
        return await original(**kwargs)

    fal.generate_lyrics = _spy
    return calls


def _break_generate_lyrics(app, exc: Exception) -> None:
    """Симулирует сбой внешнего fal-вызова (провайдер, не наш код)."""

    async def _boom(**kwargs):
        raise exc

    app.state.lyrics_service._fal.generate_lyrics = _boom


# --------------------------------------------------------------------------
# 1. 402 при нехватке средств — до fal, без списания
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lyrics_insufficient_credits_returns_402(client, app):
    """balance=0, цена lyrics=10 → 402 INSUFFICIENT_CREDITS {required:10, available:0};
    fal НЕ вызван (charge гейтит до провайдера); ledger пуст (нет списания)."""
    user_id, headers = await auth_user(client)
    await _set_lyrics_price(client, 10)
    calls = _spy_generate_lyrics(app)

    resp = await client.post(
        "/v1/lyrics", json={"prompt": "ocean song", "language": "en"}, headers=headers
    )

    assert resp.status_code == 402, resp.text
    err = resp.json()["error"]
    assert err["code"] == "INSUFFICIENT_CREDITS"
    assert err["details"] == {"required": 10, "available": 0}
    # fal не дёрнут — fail-fast до траты на провайдера
    assert calls == []
    # баланс не тронут, ledger пуст (append_ledger откатился вместе с транзакцией)
    assert await _balance(client, headers) == {"coinsAvailable": 0, "coinsReserved": 0}
    assert await _ledger(client, headers) == []


@pytest.mark.asyncio
async def test_lyrics_insufficient_when_below_price(client, app):
    """available < price → 402 без списания (частичного баланса недостаточно)."""
    user_id, headers = await auth_user(client)
    await _set_lyrics_price(client, 10)
    await _grant(client, user_id, 4)  # меньше цены
    calls = _spy_generate_lyrics(app)

    resp = await client.post(
        "/v1/lyrics", json={"prompt": "x", "language": "en"}, headers=headers
    )
    assert resp.status_code == 402, resp.text
    assert resp.json()["error"]["details"] == {"required": 10, "available": 4}
    assert calls == []
    assert await _balance(client, headers) == {"coinsAvailable": 4, "coinsReserved": 0}
    assert not any(e["kind"] == "debit_capture" for e in await _ledger(client, headers))


# --------------------------------------------------------------------------
# 2. Успешное списание
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lyrics_successful_charge_debits_price(client):
    """balance=50, цена 10 → 200 LyricsDraftResponse; balance=40; ledger debit_capture -10."""
    user_id, headers = await auth_user(client)
    await _set_lyrics_price(client, 10)
    await _grant(client, user_id, 50)

    resp = await client.post(
        "/v1/lyrics", json={"prompt": "ocean song", "language": "en"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "generated"
    assert body["content"]
    assert "id" in body

    assert await _balance(client, headers) == {"coinsAvailable": 40, "coinsReserved": 0}
    ledger = await _ledger(client, headers)
    debits = [e for e in ledger if e["kind"] == "debit_capture"]
    assert len(debits) == 1
    assert debits[0]["amount"] == -10
    # монетная модель — category не заполняется
    assert debits[0]["category"] is None


# --------------------------------------------------------------------------
# 3. Refund при сбое fal — компенсация, net 0
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lyrics_refund_on_fal_failure(client, app):
    """Сбой fal после charge → исключение (500), монеты возвращены; ledger содержит
    debit_capture -10 и credit_refund +10 (net 0)."""
    user_id, headers = await auth_user(client)
    await _set_lyrics_price(client, 10)
    await _grant(client, user_id, 50)
    _break_generate_lyrics(app, RuntimeError("fal provider is down"))

    # Отдельный клиент: не перебрасывать исключение приложения — проверяем 500-конверт
    # (unhandled_handler → INTERNAL_ERROR), при этом saga-refund уже отработал в domain.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/v1/lyrics", json={"prompt": "x", "language": "en"}, headers=headers
        )
    assert resp.status_code == 500, resp.text
    assert resp.json()["error"]["code"] == "INTERNAL_ERROR"

    # монеты возвращены — баланс восстановлен
    assert await _balance(client, headers) == {"coinsAvailable": 50, "coinsReserved": 0}
    ledger = await _ledger(client, headers)
    debits = [e for e in ledger if e["kind"] == "debit_capture"]
    refunds = [e for e in ledger if e["kind"] == "credit_refund"]
    assert len(debits) == 1 and debits[0]["amount"] == -10
    assert len(refunds) == 1 and refunds[0]["amount"] == 10
    # net списания lyrics (charge + refund) == 0 (admin-грант в сумму не входит)
    assert sum(e["amount"] for e in debits + refunds) == 0


# --------------------------------------------------------------------------
# 4. Идемпотентность по Idempotency-Key — одно списание
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lyrics_idempotent_charge_single_debit(client):
    """Два POST с одним Idempotency-Key (цена 10, balance 50) → списание ОДНО (balance 40),
    второй запрос не падает 402 даже при уже списанном балансе (dedup-first)."""
    user_id, headers = await auth_user(client)
    await _set_lyrics_price(client, 10)
    await _grant(client, user_id, 50)
    idem = {**headers, "Idempotency-Key": "lyrics-op-fixed-1"}

    r1 = await client.post(
        "/v1/lyrics", json={"prompt": "x", "language": "en"}, headers=idem
    )
    assert r1.status_code == 200, r1.text
    assert await _balance(client, headers) == {"coinsAvailable": 40, "coinsReserved": 0}

    r2 = await client.post(
        "/v1/lyrics", json={"prompt": "x", "language": "en"}, headers=idem
    )
    # ретрай тем же ключом не падает 402 и не списывает повторно
    assert r2.status_code == 200, r2.text
    assert await _balance(client, headers) == {"coinsAvailable": 40, "coinsReserved": 0}

    debits = [e for e in await _ledger(client, headers) if e["kind"] == "debit_capture"]
    assert len(debits) == 1  # ровно одно списание


@pytest.mark.asyncio
async def test_lyrics_idempotent_retry_does_not_402_on_zeroed_balance(client):
    """Баланс ровно = цене: первый charge обнуляет кошелёк; ретрай тем же ключом
    (available=0 < price) НЕ должен падать ложным 402 (dedup-first)."""
    user_id, headers = await auth_user(client)
    await _set_lyrics_price(client, 10)
    await _grant(client, user_id, 10)  # ровно на одну генерацию
    idem = {**headers, "Idempotency-Key": "lyrics-op-zero-1"}

    r1 = await client.post("/v1/lyrics", json={"prompt": "x"}, headers=idem)
    assert r1.status_code == 200, r1.text
    assert await _balance(client, headers) == {"coinsAvailable": 0, "coinsReserved": 0}

    r2 = await client.post("/v1/lyrics", json={"prompt": "x"}, headers=idem)
    assert r2.status_code == 200, r2.text  # не 402
    assert await _balance(client, headers) == {"coinsAvailable": 0, "coinsReserved": 0}
    debits = [e for e in await _ledger(client, headers) if e["kind"] == "debit_capture"]
    assert len(debits) == 1


# --------------------------------------------------------------------------
# 5. Обратная совместимость — бесплатно при отсутствии цены
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lyrics_free_when_no_price_row(client):
    """НЕТ строки lyrics в generation_prices → POST с balance=0 → 200, списания/ledger нет.
    (Изоляция: цену НЕ ставим; clean_db всё равно пересевает прайс перед каждым тестом.)"""
    _user_id, headers = await auth_user(client)

    resp = await client.post(
        "/v1/lyrics", json={"prompt": "free theme", "language": "en"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["source"] == "generated"
    # ни списания, ни строки ledger (charge вернул 0, append_ledger не вызывался)
    assert await _balance(client, headers) == {"coinsAvailable": 0, "coinsReserved": 0}
    assert await _ledger(client, headers) == []


@pytest.mark.asyncio
async def test_lyrics_free_when_price_zero(client):
    """price_coins=0 → бесплатно (charge ранний return 0, ledger не пишется)."""
    _user_id, headers = await auth_user(client)
    await _set_lyrics_price(client, 0)

    resp = await client.post("/v1/lyrics", json={"prompt": "x"}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert await _balance(client, headers) == {"coinsAvailable": 0, "coinsReserved": 0}
    assert await _ledger(client, headers) == []


# --------------------------------------------------------------------------
# 6. get / patch не биллятся (list HTTP-роута у lyrics нет)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lyrics_get_and_patch_do_not_bill(client):
    """Чтение (GET) и редактирование (PATCH) драфта не трогают баланс."""
    user_id, headers = await auth_user(client)
    await _set_lyrics_price(client, 10)
    await _grant(client, user_id, 50)

    created = await client.post(
        "/v1/lyrics", json={"prompt": "x", "language": "en"}, headers=headers
    )
    assert created.status_code == 200, created.text
    draft_id = created.json()["id"]
    # после единственного платного POST
    assert await _balance(client, headers) == {"coinsAvailable": 40, "coinsReserved": 0}

    # GET не биллится
    got = await client.get(f"/v1/lyrics/{draft_id}", headers=headers)
    assert got.status_code == 200, got.text
    assert await _balance(client, headers) == {"coinsAvailable": 40, "coinsReserved": 0}

    # PATCH (редактирование, fal не вызывается) не биллится
    patched = await client.patch(
        f"/v1/lyrics/{draft_id}", json={"content": "my edited lyrics"}, headers=headers
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["source"] == "edited"
    assert await _balance(client, headers) == {"coinsAvailable": 40, "coinsReserved": 0}

    # ровно одно списание за весь сценарий (только POST)
    debits = [e for e in await _ledger(client, headers) if e["kind"] == "debit_capture"]
    assert len(debits) == 1
