from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models.billing import Product, Purchase, SubscriptionState


class ProductsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_external_id(self, external_id: str) -> Product | None:
        stmt = select(Product).where(Product.external_product_id == external_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_active(self) -> list[Product]:
        stmt = select(Product).where(Product.active.is_(True))
        return list((await self._session.execute(stmt)).scalars().all())


class PurchasesRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        user_id: UUID,
        product_external_id: str,
        transaction_id: str,
        original_transaction_id: str | None,
        dedup_key: str,
        environment: str,
        purchase_date: datetime | None,
        raw: dict[str, Any] | None,
    ) -> bool:
        """Записывает покупку. True — новая (False — ключ `dedup_key` уже занят).

        Дедуп идёт по `uq_purchases_dedup_key` (ADR-013 D2). False означает лишь, что ключ
        занят — НЕ обязательно этим же пользователем; владельца отдаёт `find_owner_by_dedup_key`.
        """
        stmt = (
            pg_insert(Purchase)
            .values(
                user_id=user_id,
                product_external_id=product_external_id,
                transaction_id=transaction_id,
                original_transaction_id=original_transaction_id,
                dedup_key=dedup_key,
                environment=environment,
                purchase_date=purchase_date,
                status="applied",
                raw=raw,
            )
            .on_conflict_do_nothing(constraint="uq_purchases_dedup_key")
            .returning(Purchase.id)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None

    async def find_owner_by_dedup_key(self, dedup_key: str) -> UUID | None:
        """Владелец уже применённой покупки с этим дедуп-ключом (для честного ответа при дедупе)."""
        stmt = select(Purchase.user_id).where(Purchase.dedup_key == dedup_key).limit(1)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def find_user_by_original_transaction(
        self, original_transaction_id: str
    ) -> UUID | None:
        stmt = (
            select(Purchase.user_id)
            .where(Purchase.original_transaction_id == original_transaction_id)
            .limit(1)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is not None:
            return row
        stmt2 = (
            select(SubscriptionState.user_id)
            .where(SubscriptionState.original_transaction_id == original_transaction_id)
            .limit(1)
        )
        return (await self._session.execute(stmt2)).scalar_one_or_none()
