from __future__ import annotations

import time
import uuid

import pytest

from tests.helpers import auth_headers, auth_user, make_signed_transaction

ADMIN = {"Authorization": "Bearer test-admin-key"}


def _weekly_tx(transaction_id: str) -> str:
    return make_signed_transaction(
        product_id="com.musicfy.sub.weekly",
        transaction_id=transaction_id,
        expires_date_ms=int((time.time() + 7 * 86400) * 1000),
    )


async def _balance(client, headers) -> dict:
    return (await client.get("/v1/billing/balance", headers=headers)).json()


# --------------------------------------------------------------------------
# API-контракты (монетные формы)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_balance_returns_coin_wallet_shape(client):
    headers = await auth_headers(client)
    bal = await _balance(client, headers)
    assert bal == {"coinsAvailable": 0, "coinsReserved": 0}


@pytest.mark.asyncio
async def test_pricing_lists_active_paid_types(client):
    resp = await client.get("/v1/billing/pricing")
    assert resp.status_code == 200
    prices = {p["jobType"]: p["priceCoins"] for p in resp.json()["prices"]}
    assert prices == {"song": 10, "cover": 5, "video": 30}
    # бесплатные типы (цена 0) в прайс-листе не появляются
    assert "lyrics" not in prices
    assert "voice_clone" not in prices


@pytest.mark.asyncio
async def test_products_return_coin_grants(client):
    resp = await client.get("/v1/billing/products")
    assert resp.status_code == 200
    products = {p["productId"]: p for p in resp.json()}
    assert products["com.musicfy.coins.small"]["grants"] == {"coins": 100}
    assert products["com.musicfy.coins.medium"]["grants"] == {"coins": 550}
    assert products["com.musicfy.coins.large"]["grants"] == {"coins": 1200}
    assert products["com.musicfy.coins.xl"]["grants"] == {"coins": 3000}
    assert products["com.musicfy.coins.small"]["kind"] == "coin_pack"
    weekly = products["com.musicfy.sub.weekly"]
    assert weekly["grants"] == {"coins": 150}
    assert weekly["periodDays"] == 7
    assert products["com.musicfy.sub.yearly"]["grants"] == {"coins": 8000}


# --------------------------------------------------------------------------
# Paywall / недостаток средств
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paywall_without_coins(client):
    headers = await auth_headers(client)
    resp = await client.post("/v1/songs", json={"prompt": "x"}, headers=headers)
    assert resp.status_code == 402
    body = resp.json()["error"]
    assert body["code"] == "INSUFFICIENT_CREDITS"
    # details содержат монетную форму {required, available}
    assert body["details"]["required"] == 10
    assert body["details"]["available"] == 0
    # генерация не стартовала — баланс не тронут
    assert await _balance(client, headers) == {"coinsAvailable": 0, "coinsReserved": 0}


@pytest.mark.asyncio
async def test_insufficient_when_below_price(client):
    """available < price → 402 с деталями, резерв не выполняется."""
    user_id, headers = await auth_user(client)
    # даём 4 монеты — меньше цены cover (5)
    await client.post(
        f"/v1/admin/users/{user_id}/credits", json={"coins": 4}, headers=ADMIN
    )
    resp = await client.post(
        "/v1/covers", json={"source_audio_url": "https://cdn.local/in.mp3"}, headers=headers
    )
    assert resp.status_code == 402
    details = resp.json()["error"]["details"]
    assert details == {"required": 5, "available": 4}
    assert await _balance(client, headers) == {"coinsAvailable": 4, "coinsReserved": 0}


# --------------------------------------------------------------------------
# reserve / capture / release по прайс-листу
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reserve_then_capture_debits_price(client, app):
    """Успешная генерация: reserve резервирует цену, при завершении capture списывает."""
    from tests.helpers import emit_fal_completed, provider_request_id

    user_id, headers = await auth_user(client)
    await client.post(
        f"/v1/admin/users/{user_id}/credits", json={"coins": 50}, headers=ADMIN
    )

    job_id = (
        await client.post("/v1/songs", json={"prompt": "x"}, headers=headers)
    ).json()["jobId"]

    # во время выполнения: song=10 зарезервировано
    running = await _balance(client, headers)
    assert running == {"coinsAvailable": 40, "coinsReserved": 10}

    rid = await provider_request_id(app, job_id)
    await emit_fal_completed(client, rid, media_url="https://cdn.local/s.mp3", duration=30)

    final = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
    assert final["status"] == "completed"
    # после capture: 10 списано, reserved обнулён
    assert await _balance(client, headers) == {"coinsAvailable": 40, "coinsReserved": 0}

    ledger = (await client.get("/v1/billing/ledger", headers=headers)).json()
    kinds = [e["kind"] for e in ledger]
    assert "debit_reserve" in kinds
    assert "debit_capture" in kinds
    # category в монетной модели не заполняется
    assert all(e["category"] is None for e in ledger)


@pytest.mark.asyncio
async def test_release_refunds_reserved_on_failure(client, app):
    """Провал генерации → reserved возвращается в available (release)."""
    from tests.helpers import emit_fal_error, provider_request_id

    user_id, headers = await auth_user(client)
    await client.post(
        f"/v1/admin/users/{user_id}/credits", json={"coins": 50}, headers=ADMIN
    )

    job_id = (
        await client.post("/v1/songs", json={"prompt": "x"}, headers=headers)
    ).json()["jobId"]
    assert (await _balance(client, headers))["coinsReserved"] == 10

    # song-пайплайн делает music-fallback: терминальный failed нужен вторым ERROR
    rid1 = await provider_request_id(app, job_id)
    await emit_fal_error(client, rid1, error="primary failed")
    rid2 = await provider_request_id(app, job_id)
    await emit_fal_error(client, rid2, error="fallback failed")

    final = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
    assert final["status"] == "failed"
    # refund: полный возврат, баланс восстановлен
    assert await _balance(client, headers) == {"coinsAvailable": 50, "coinsReserved": 0}

    ledger = (await client.get("/v1/billing/ledger", headers=headers)).json()
    assert any(e["kind"] == "credit_release" for e in ledger)


@pytest.mark.asyncio
async def test_free_types_do_not_reserve(client):
    """lyrics/voice_clone (цена 0) доступны без баланса, резерв не выполняется."""
    headers = await auth_headers(client)
    # lyrics: без монет генерация текста проходит
    gen = await client.post(
        "/v1/lyrics", json={"prompt": "ocean song", "language": "en"}, headers=headers
    )
    assert gen.status_code == 200, gen.text
    # voice_clone: без монет создаётся voice profile
    consent = await client.post(
        "/v1/voices/consent",
        json={"kind": "own_voice", "accepted": True, "statement": "my voice"},
        headers=headers,
    )
    voice = await client.post(
        "/v1/voices",
        json={"sampleAssetUrl": "https://cdn.local/v.wav", "consentId": consent.json()["id"]},
        headers=headers,
    )
    assert voice.status_code == 201, voice.text
    # баланс не тронут — резерва не было
    assert await _balance(client, headers) == {"coinsAvailable": 0, "coinsReserved": 0}
    ledger = (await client.get("/v1/billing/ledger", headers=headers)).json()
    assert not any(e["kind"] == "debit_reserve" for e in ledger)


# --------------------------------------------------------------------------
# Покупки: начисление монет + идемпотентность по transaction_id
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coin_pack_purchase_credits_wallet(client):
    headers = await auth_headers(client)
    signed = make_signed_transaction(
        product_id="com.musicfy.coins.medium", transaction_id="tx-pack-medium"
    )
    r = await client.post(
        "/v1/billing/purchases/verify", json={"signedTransaction": signed}, headers=headers
    )
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert await _balance(client, headers) == {"coinsAvailable": 550, "coinsReserved": 0}

    ledger = (await client.get("/v1/billing/ledger", headers=headers)).json()
    assert any(e["kind"] == "credit_purchase" for e in ledger)


@pytest.mark.asyncio
async def test_pack_purchase_idempotent_by_transaction(client):
    """Повторный verify пака с тем же transaction_id → монеты начислены ОДИН раз."""
    headers = await auth_headers(client)
    signed = make_signed_transaction(
        product_id="com.musicfy.coins.small", transaction_id="tx-pack-dup"
    )
    r1 = await client.post(
        "/v1/billing/purchases/verify", json={"signedTransaction": signed}, headers=headers
    )
    assert r1.json()["deduplicated"] is False
    r2 = await client.post(
        "/v1/billing/purchases/verify", json={"signedTransaction": signed}, headers=headers
    )
    assert r2.json()["deduplicated"] is True
    # начислено ровно 100 (не 200)
    assert await _balance(client, headers) == {"coinsAvailable": 100, "coinsReserved": 0}


@pytest.mark.asyncio
async def test_subscription_purchase_idempotent_by_transaction(client):
    """Правка 2: повторный verify подписки с тем же tx → нет двойного начисления."""
    headers = await auth_headers(client)
    signed = _weekly_tx("tx-sub-dup")
    r1 = await client.post(
        "/v1/billing/purchases/verify", json={"signedTransaction": signed}, headers=headers
    )
    assert r1.json()["deduplicated"] is False
    r2 = await client.post(
        "/v1/billing/purchases/verify", json={"signedTransaction": signed}, headers=headers
    )
    assert r2.json()["deduplicated"] is True
    # подписка начисляет 150 монет ровно один раз
    assert await _balance(client, headers) == {"coinsAvailable": 150, "coinsReserved": 0}
    ledger = (await client.get("/v1/billing/ledger", headers=headers)).json()
    grants = [e for e in ledger if e["kind"] == "credit_subscription_grant"]
    assert len(grants) == 1


@pytest.mark.asyncio
async def test_restore_is_idempotent(client):
    """restore тех же транзакций не начисляет монеты повторно."""
    headers = await auth_headers(client)
    signed = make_signed_transaction(
        product_id="com.musicfy.coins.small", transaction_id="tx-restore-1"
    )
    await client.post(
        "/v1/billing/purchases/verify", json={"signedTransaction": signed}, headers=headers
    )
    resp = await client.post(
        "/v1/billing/restore", json={"signedTransactions": [signed]}, headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()[0]["deduplicated"] is True
    assert await _balance(client, headers) == {"coinsAvailable": 100, "coinsReserved": 0}


# --------------------------------------------------------------------------
# capture / release идемпотентны по job.id (через дубликат webhook)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_idempotent_on_duplicate_webhook(client, app):
    """Повторный webhook завершения не списывает монеты дважды (capture идемпотентен)."""
    from tests.helpers import emit_fal_completed, provider_request_id

    user_id, headers = await auth_user(client)
    await client.post(
        f"/v1/admin/users/{user_id}/credits", json={"coins": 50}, headers=ADMIN
    )
    job_id = (
        await client.post("/v1/songs", json={"prompt": "x"}, headers=headers)
    ).json()["jobId"]
    rid = await provider_request_id(app, job_id)

    first = await emit_fal_completed(client, rid, media_url="https://cdn.local/a.mp3", duration=10)
    assert first.json()["status"] == "ok"
    second = await emit_fal_completed(client, rid, media_url="https://cdn.local/a.mp3", duration=10)
    assert second.json()["status"] == "duplicate"

    # capture списал 10 ровно один раз
    assert await _balance(client, headers) == {"coinsAvailable": 40, "coinsReserved": 0}
    ledger = (await client.get("/v1/billing/ledger", headers=headers)).json()
    assert len([e for e in ledger if e["kind"] == "debit_capture"]) == 1


# --------------------------------------------------------------------------
# merge монет: guest → apple sign-in
# --------------------------------------------------------------------------


class _FakeAppleVerifier:
    async def verify(self, identity_token, *, nonce=None):
        return {"sub": identity_token}

    async def aclose(self):
        pass


def _install_fake_apple(app):
    from app.auth.sessions import AuthService

    app.state.auth_service = AuthService(
        app.state.sessionmaker, apple_verifier=_FakeAppleVerifier(), session_ttl_seconds=3600
    )


@pytest.mark.asyncio
async def test_guest_coins_preserved_on_first_apple_sign_in(client, app):
    """Гость с монетами впервые входит через Apple (промоут гостя) — монеты сохраняются."""
    _install_fake_apple(app)
    guest = (await client.post("/v1/auth/guest", json={})).json()
    gh = {"Authorization": f"Bearer {guest['token']}"}
    await client.post(
        "/v1/billing/purchases/verify",
        json={"signedTransaction": make_signed_transaction(
            product_id="com.musicfy.coins.small", transaction_id="tx-merge-promote")},
        headers=gh,
    )
    apple = (
        await client.post(
            "/v1/auth/apple", json={"identityToken": f"sub-new-{uuid.uuid4()}"}, headers=gh
        )
    ).json()
    ah = {"Authorization": f"Bearer {apple['token']}"}
    assert await _balance(client, ah) == {"coinsAvailable": 100, "coinsReserved": 0}


@pytest.mark.asyncio
async def test_guest_coins_merge_into_existing_apple_wallet(client, app):
    """Был баг: guest→apple при существующем Apple-аккаунте — монеты гостя суммируются
    в целевой кошелёк, гостевой кошелёк удаляется."""
    _install_fake_apple(app)
    apple_sub = f"sub-existing-{uuid.uuid4()}"

    # 1. существующий Apple-аккаунт с монетами (пак medium = 550)
    target = (
        await client.post("/v1/auth/apple", json={"identityToken": apple_sub})
    ).json()
    th = {"Authorization": f"Bearer {target['token']}"}
    await client.post(
        "/v1/billing/purchases/verify",
        json={"signedTransaction": make_signed_transaction(
            product_id="com.musicfy.coins.medium", transaction_id="tx-target-550")},
        headers=th,
    )
    assert (await _balance(client, th))["coinsAvailable"] == 550

    # 2. отдельный гость с монетами (пак small = 100)
    guest = (await client.post("/v1/auth/guest", json={})).json()
    gh = {"Authorization": f"Bearer {guest['token']}"}
    await client.post(
        "/v1/billing/purchases/verify",
        json={"signedTransaction": make_signed_transaction(
            product_id="com.musicfy.coins.small", transaction_id="tx-guest-100")},
        headers=gh,
    )

    # 3. гость входит под существующим Apple sub → merge гостя в целевой аккаунт
    linked = (
        await client.post(
            "/v1/auth/apple", json={"identityToken": apple_sub}, headers=gh
        )
    ).json()
    lh = {"Authorization": f"Bearer {linked['token']}"}
    # монеты суммированы: 550 + 100 = 650
    assert await _balance(client, lh) == {"coinsAvailable": 650, "coinsReserved": 0}

    # гостевой кошелёк удалён — по старому токену гостя кошелёк более не резолвится
    # (сессия гостя переназначена на целевого юзера, баланс равен merged)
    assert (await _balance(client, gh))["coinsAvailable"] == 650
