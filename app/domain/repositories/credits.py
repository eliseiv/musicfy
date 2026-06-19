from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import CreditCategory, CreditLedgerKind, CreditSource
from app.domain.models.billing import (
    CreditBalance,
    CreditLedgerEntry,
    Entitlement,
    SubscriptionState,
)


class CreditsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- entitlements ---

    async def get_entitlement_for_update(
        self, *, user_id: UUID, category: CreditCategory
    ) -> Entitlement | None:
        stmt = (
            select(Entitlement)
            .where(Entitlement.user_id == user_id, Entitlement.category == category)
            .with_for_update()
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_entitlements(self, user_id: UUID) -> list[Entitlement]:
        stmt = select(Entitlement).where(Entitlement.user_id == user_id)
        return list((await self._session.execute(stmt)).scalars().all())

    async def upsert_entitlement(
        self,
        *,
        user_id: UUID,
        category: CreditCategory,
        granted: int,
        period_start: datetime | None,
        period_end: datetime | None,
        source_product_external_id: str | None,
    ) -> None:
        stmt = pg_insert(Entitlement).values(
            user_id=user_id,
            category=category,
            granted=granted,
            used=0,
            period_start=period_start,
            period_end=period_end,
            source_product_external_id=source_product_external_id,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_entitlements_user_id_category",
            set_={
                "granted": granted,
                "used": 0,
                "period_start": period_start,
                "period_end": period_end,
                "source_product_external_id": source_product_external_id,
            },
        )
        await self._session.execute(stmt)

    # --- balances ---

    async def get_balance_for_update(
        self, *, user_id: UUID, category: CreditCategory
    ) -> CreditBalance | None:
        stmt = (
            select(CreditBalance)
            .where(CreditBalance.user_id == user_id, CreditBalance.category == category)
            .with_for_update()
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_balances(self, user_id: UUID) -> list[CreditBalance]:
        stmt = select(CreditBalance).where(CreditBalance.user_id == user_id)
        return list((await self._session.execute(stmt)).scalars().all())

    async def ensure_balance(
        self, *, user_id: UUID, category: CreditCategory
    ) -> CreditBalance:
        bal = await self.get_balance_for_update(user_id=user_id, category=category)
        if bal is None:
            bal = CreditBalance(user_id=user_id, category=category, available=0, reserved=0)
            self._session.add(bal)
            await self._session.flush()
        return bal

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
        """Добавляет запись в ledger. Возвращает True если новая (False — дубликат)."""
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

    async def upsert_subscription(self, *, user_id: UUID, values: dict[str, Any]) -> None:
        stmt = pg_insert(SubscriptionState).values(user_id=user_id, **values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_subscription_state_user_id", set_=values
        )
        await self._session.execute(stmt)
