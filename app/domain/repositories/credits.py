from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    CreditCategory,
    CreditLedgerKind,
    CreditSource,
    SubscriptionStatus,
)
from app.domain.models.billing import (
    CoinWallet,
    CreditLedgerEntry,
    SubscriptionState,
)


class CreditsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- coin wallet ---

    async def get_wallet(self, user_id: UUID) -> CoinWallet | None:
        stmt = select(CoinWallet).where(CoinWallet.user_id == user_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_wallet_for_update(self, user_id: UUID) -> CoinWallet | None:
        stmt = (
            select(CoinWallet)
            .where(CoinWallet.user_id == user_id)
            .with_for_update()
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def ensure_wallet(self, user_id: UUID) -> CoinWallet:
        """Возвращает строку кошелька под FOR UPDATE, создавая её при отсутствии."""
        wallet = await self.get_wallet_for_update(user_id)
        if wallet is None:
            # on_conflict_do_nothing защищает от гонки при первом обращении.
            insert_stmt = (
                pg_insert(CoinWallet)
                .values(user_id=user_id, coins_available=0, coins_reserved=0)
                .on_conflict_do_nothing(constraint="uq_coin_wallets_user_id")
            )
            await self._session.execute(insert_stmt)
            wallet = await self.get_wallet_for_update(user_id)
            assert wallet is not None
        return wallet

    # --- ledger ---

    async def append_ledger(
        self,
        *,
        user_id: UUID,
        kind: CreditLedgerKind,
        amount: int,
        category: CreditCategory | None = None,
        source: CreditSource | None = None,
        ref_type: str | None = None,
        ref_id: str | None = None,
        reason: str | None = None,
        idempotency_key: str | None = None,
    ) -> bool:
        """Добавляет запись в ledger. Возвращает True если новая (False — дубликат).

        `category` в монетной модели больше не заполняется (пишется NULL); параметр
        оставлен для совместимости, но новые записи категорию не указывают.
        """
        stmt = pg_insert(CreditLedgerEntry).values(
            user_id=user_id,
            kind=kind,
            amount=amount,
            category=category,
            source=source,
            ref_type=ref_type,
            ref_id=ref_id,
            reason=reason,
            idempotency_key=idempotency_key,
        )
        if idempotency_key is not None:
            stmt = stmt.on_conflict_do_nothing(
                constraint="uq_credit_ledger_idempotency_key"
            ).returning(CreditLedgerEntry.id)
            result = await self._session.execute(stmt)
            return result.scalar_one_or_none() is not None
        await self._session.execute(stmt)
        return True

    async def list_ledger(
        self, *, user_id: UUID, limit: int = 100, offset: int = 0
    ) -> list[CreditLedgerEntry]:
        stmt = (
            select(CreditLedgerEntry)
            .where(CreditLedgerEntry.user_id == user_id)
            .order_by(CreditLedgerEntry.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    # --- subscription ---

    async def get_subscription_for_update(self, user_id: UUID) -> SubscriptionState | None:
        stmt = (
            select(SubscriptionState)
            .where(SubscriptionState.user_id == user_id)
            .with_for_update()
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_subscription(self, user_id: UUID) -> SubscriptionState | None:
        stmt = select(SubscriptionState).where(SubscriptionState.user_id == user_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def expire_foreign_subscriptions(
        self, *, original_transaction_id: str, except_user_id: UUID
    ) -> int:
        """Гасит активные подписки ДРУГИХ пользователей на ту же Apple-цепочку.

        Одна подписка Apple ID (`original_transaction_id`) — один активный владелец:
        при переносе entitlement прежний владелец теряет статус `active` (→ `expired`).
        """
        stmt = (
            update(SubscriptionState)
            .where(
                SubscriptionState.original_transaction_id == original_transaction_id,
                SubscriptionState.user_id != except_user_id,
                SubscriptionState.status == SubscriptionStatus.active,
            )
            .values(status=SubscriptionStatus.expired)
        )
        return (await self._session.execute(stmt)).rowcount

    async def upsert_subscription(self, *, user_id: UUID, values: dict[str, Any]) -> None:
        stmt = pg_insert(SubscriptionState).values(user_id=user_id, **values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_subscription_state_user_id", set_=values
        )
        await self._session.execute(stmt)
