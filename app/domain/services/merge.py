"""Перенос данных guest → постоянный аккаунт при Sign in with Apple.

Регистрирует reassign-корутины в app.auth.sessions.MERGE_REASSIGNERS. Импортируется
в app.main при старте, чтобы merge учитывал доменные таблицы.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.sessions import MERGE_REASSIGNERS

# Таблицы с простым владением (user_id) — просто переназначаем владельца.
_SIMPLE_OWNED = (
    "jobs", "tracks", "lyrics_drafts", "assets", "credit_ledger", "purchases",
    "voice_consents", "voice_profiles", "device_push_tokens", "usage_events",
)


async def _reassign_simple(session: AsyncSession, frm: UUID, to: UUID) -> None:
    for table in _SIMPLE_OWNED:
        await session.execute(
            text(f"UPDATE {table} SET user_id = :to WHERE user_id = :frm"),
            {"to": to, "frm": frm},
        )


async def _merge_coin_wallets(session: AsyncSession, frm: UUID, to: UUID) -> None:
    # Переносим монеты гостя в кошелёк целевого аккаунта: суммируем available и
    # reserved, затем удаляем гостевой кошелёк. Идемпотентно — после удаления
    # гостевой строки повторный вызов ничего не переносит.
    await session.execute(
        text(
            """
            INSERT INTO coin_wallets (user_id, coins_available, coins_reserved)
            SELECT :to, coins_available, coins_reserved
            FROM coin_wallets WHERE user_id = :frm
            ON CONFLICT (user_id) DO UPDATE
            SET coins_available = coin_wallets.coins_available + EXCLUDED.coins_available,
                coins_reserved = coin_wallets.coins_reserved + EXCLUDED.coins_reserved,
                updated_at = now()
            """
        ),
        {"to": to, "frm": frm},
    )
    await session.execute(
        text("DELETE FROM coin_wallets WHERE user_id = :frm"), {"frm": frm}
    )


async def reassign_all(session: AsyncSession, frm: UUID, to: UUID) -> None:
    await _reassign_simple(session, frm, to)
    await _merge_coin_wallets(session, frm, to)


def register() -> None:
    if reassign_all not in MERGE_REASSIGNERS:
        MERGE_REASSIGNERS.append(reassign_all)
