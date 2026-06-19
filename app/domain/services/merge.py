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


async def _merge_credit_balances(session: AsyncSession, frm: UUID, to: UUID) -> None:
    # Суммируем покупные кредиты гостя в аккаунт назначения по категориям.
    await session.execute(
        text(
            """
            INSERT INTO credit_balances (user_id, category, available, reserved)
            SELECT :to, category, available, reserved FROM credit_balances WHERE user_id = :frm
            ON CONFLICT (user_id, category) DO UPDATE
            SET available = credit_balances.available + EXCLUDED.available,
                reserved = credit_balances.reserved + EXCLUDED.reserved
            """
        ),
        {"to": to, "frm": frm},
    )
    await session.execute(
        text("DELETE FROM credit_balances WHERE user_id = :frm"), {"frm": frm}
    )


async def _merge_entitlements(session: AsyncSession, frm: UUID, to: UUID) -> None:
    # Entitlements переносим только для категорий, которых нет у целевого аккаунта
    # (у постоянного аккаунта подписка приоритетнее гостевой).
    await session.execute(
        text(
            """
            INSERT INTO entitlements
                (user_id, category, granted, used, period_start, period_end,
                 source_product_external_id)
            SELECT :to, category, granted, used, period_start, period_end,
                   source_product_external_id
            FROM entitlements WHERE user_id = :frm
            ON CONFLICT (user_id, category) DO NOTHING
            """
        ),
        {"to": to, "frm": frm},
    )
    await session.execute(
        text("DELETE FROM entitlements WHERE user_id = :frm"), {"frm": frm}
    )


async def reassign_all(session: AsyncSession, frm: UUID, to: UUID) -> None:
    await _reassign_simple(session, frm, to)
    await _merge_credit_balances(session, frm, to)
    await _merge_entitlements(session, frm, to)


def register() -> None:
    if reassign_all not in MERGE_REASSIGNERS:
        MERGE_REASSIGNERS.append(reassign_all)
