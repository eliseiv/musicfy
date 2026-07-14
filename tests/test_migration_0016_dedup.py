"""ADR-013 — миграция `0016_purchase_dedup_key`: реальный round-trip схемы и данных.

Проверяется на живой БД (isolated single-process):
  * бэкфилл `purchases.dedup_key = 'Production:' || transaction_id` (шаг 3);
  * шаг 6 переводит ТОЛЬКО `purchase:{tx}` (ref_type='transaction') → `purchase:Production:{tx}`,
    НЕ трогая посторонние потоки `lyrics:*` / `manual:*`;
  * downgrade откатывает ключ леджера, upgrade восстанавливает (round-trip);
  * уже применённый боевой чек после миграции НЕ начисляется повторно (double-grant предотвращён).

Тест мутирует схему (downgrade/upgrade), поэтому запускать ИЗОЛИРОВАННО одним процессом.
`finally` гарантирует возврат к head при любом исходе.
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import jwt
import pytest
from sqlalchemy import text

from app.config import get_settings
from app.db.session import build_engine, build_sessionmaker
from app.domain.providers.billing.apple import AppleStoreKitVerifier
from app.domain.services.billing_service import BillingService

_REPO = Path(__file__).resolve().parents[1]


def _alembic(*args: str) -> None:
    # Вызываем alembic как модуль тем же интерпретатором — кросс-платформенно
    # (не зависит от имени бинарника alembic/alembic.exe, работает на Windows и Linux-CI).
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(_REPO / "alembic.ini"), *args],
        cwd=str(_REPO),
        env=dict(os.environ),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"alembic {' '.join(args)} failed (rc={result.returncode}):\n"
            f"{result.stdout}\n{result.stderr}"
        )


async def _scalar(sql: str, params: dict):
    """Читает один скаляр на свежем engine и сразу его закрывает (не держим соединений)."""
    engine = build_engine(get_settings())
    sessionmaker = build_sessionmaker(engine)
    try:
        async with sessionmaker() as session:
            return (await session.execute(text(sql), params)).scalar_one()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_0016_roundtrip_and_no_double_grant():
    user_id = str(uuid.uuid4())
    tx = "boevoy-777"
    p = {"u": user_id, "tx": tx}

    # Пин схемы к ЯВНОЙ ревизии 0016 перед сидом — тест целится в границу 0016, а не в
    # относительные -1/head (которые «съезжают» при добавлении миграций поверх 0016).
    # На head это no-op (0016 уже применена); если БД ниже — доведёт ровно до 0016.
    _alembic("upgrade", "0016_purchase_dedup_key")

    # --- seed на 0016: применённый боевой чек + посторонние ledger-потоки ---
    engine = build_engine(get_settings())
    sessionmaker = build_sessionmaker(engine)
    async with sessionmaker() as session:
        async with session.begin():
            await session.execute(
                text("INSERT INTO users (id, is_guest) VALUES (CAST(:u AS uuid), true)"), p
            )
            await session.execute(
                text(
                    "INSERT INTO purchases (user_id, product_external_id, transaction_id, "
                    "dedup_key, environment, status) VALUES (CAST(:u AS uuid), "
                    "'com.musicfy.coins.small', :tx, :dk, 'Production', 'applied')"
                ),
                {**p, "dk": f"Production:{tx}"},
            )
            await session.execute(
                text(
                    "INSERT INTO credit_ledger (user_id, kind, amount, ref_type, ref_id, "
                    "idempotency_key) VALUES (CAST(:u AS uuid), 'credit_purchase', 100, "
                    "'transaction', :tx, :ik)"
                ),
                {**p, "ik": f"purchase:Production:{tx}"},
            )
            # посторонние потоки: их шаг 6 трогать не должен
            await session.execute(
                text(
                    "INSERT INTO credit_ledger (user_id, kind, amount, ref_type, ref_id, "
                    "idempotency_key) VALUES (CAST(:u AS uuid), 'debit_capture', -5, "
                    "'job', 'job-1', 'lyrics:job-1')"
                ),
                p,
            )
            await session.execute(
                text(
                    "INSERT INTO credit_ledger (user_id, kind, amount, idempotency_key) "
                    "VALUES (CAST(:u AS uuid), 'credit_adjustment', 50, 'manual:dev-grant')"
                ),
                p,
            )
            await session.execute(
                text(
                    "INSERT INTO coin_wallets (user_id, coins_available, coins_reserved) "
                    "VALUES (CAST(:u AS uuid), 100, 0)"
                ),
                p,
            )
    await engine.dispose()

    try:
        # --- downgrade через границу 0016 (к явной 0015): ключ боевого гранта откатан к
        #     старому формату. Целимся в 0015_soft_delete, а не в -1: так 0016.downgrade
        #     реально выполнится независимо от того, сколько миграций накатано поверх 0016. ---
        _alembic("downgrade", "0015_soft_delete")
        after_down = await _scalar(
            "SELECT idempotency_key FROM credit_ledger "
            "WHERE ref_type='transaction' AND user_id=CAST(:u AS uuid)",
            p,
        )
        assert after_down == f"purchase:{tx}"
        # посторонние потоки downgrade не тронул
        assert (
            await _scalar(
                "SELECT idempotency_key FROM credit_ledger "
                "WHERE idempotency_key LIKE 'lyrics:%' AND user_id=CAST(:u AS uuid)",
                p,
            )
            == "lyrics:job-1"
        )

        # --- upgrade 0015 -> 0016 (явная ревизия): backfill dedup_key + шаг 6 восстанавливает
        #     ключ. Останавливаемся ровно на 0016, чтобы проверить именно её backfill; общий
        #     возврат к head — в finally. ---
        _alembic("upgrade", "0016_purchase_dedup_key")
        assert (
            await _scalar(
                "SELECT dedup_key FROM purchases WHERE user_id=CAST(:u AS uuid)", p
            )
            == f"Production:{tx}"
        )
        assert (
            await _scalar(
                "SELECT idempotency_key FROM credit_ledger "
                "WHERE ref_type='transaction' AND user_id=CAST(:u AS uuid)",
                p,
            )
            == f"purchase:Production:{tx}"
        )
        # посторонние потоки round-trip'ом не затронуты
        assert (
            await _scalar(
                "SELECT idempotency_key FROM credit_ledger "
                "WHERE idempotency_key LIKE 'manual:%' AND user_id=CAST(:u AS uuid)",
                p,
            )
            == "manual:dev-grant"
        )
        assert (
            await _scalar(
                "SELECT idempotency_key FROM credit_ledger "
                "WHERE idempotency_key LIKE 'lyrics:%' AND user_id=CAST(:u AS uuid)",
                p,
            )
            == "lyrics:job-1"
        )

        # --- double-grant предотвращён: повторный verify боевого чека не начисляет ---
        engine = build_engine(get_settings())
        sessionmaker = build_sessionmaker(engine)
        try:
            service = BillingService(
                sessionmaker,
                verifier=AppleStoreKitVerifier(
                    bundle_id="com.musicfy.app", verify_signature=False
                ),
            )
            token = jwt.encode(
                {
                    "transactionId": tx,
                    "originalTransactionId": tx,
                    "productId": "com.musicfy.coins.small",
                    "environment": "Production",
                },
                "test-key",
                algorithm="HS256",
            )
            result = await service.verify_and_apply_transaction(
                user_id=uuid.UUID(user_id), signed_transaction=token
            )
            assert result["status"] == "ok"
            assert result["deduplicated"] is True
        finally:
            await engine.dispose()

        coins = await _scalar(
            "SELECT coins_available FROM coin_wallets WHERE user_id=CAST(:u AS uuid)", p
        )
        assert coins == 100  # НЕ 200 — повторного начисления боевого чека не произошло
    finally:
        _alembic("upgrade", "head")
