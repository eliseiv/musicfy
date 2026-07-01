from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models.billing import GenerationPrice


class PricingRepository:
    """Чтение/обновление прайс-листа генераций (`generation_prices`)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_active_price(self, job_type: str) -> int | None:
        """Цена активного типа в монетах или None (неизвестный/неактивный → бесплатно)."""
        stmt = select(GenerationPrice.price_coins).where(
            GenerationPrice.job_type == job_type,
            GenerationPrice.active.is_(True),
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_active(self) -> list[GenerationPrice]:
        stmt = (
            select(GenerationPrice)
            .where(GenerationPrice.active.is_(True))
            .order_by(GenerationPrice.job_type)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def upsert_price(
        self, *, job_type: str, price_coins: int, active: bool
    ) -> GenerationPrice:
        """Создаёт/обновляет строку прайс-листа. Возвращает актуальную запись."""
        stmt = (
            pg_insert(GenerationPrice)
            .values(job_type=job_type, price_coins=price_coins, active=active)
            .on_conflict_do_update(
                constraint="uq_generation_prices_job_type",
                set_={"price_coins": price_coins, "active": active},
            )
        )
        await self._session.execute(stmt)
        row = (
            await self._session.execute(
                select(GenerationPrice).where(GenerationPrice.job_type == job_type)
            )
        ).scalar_one()
        return row
