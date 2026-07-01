from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.errors import InsufficientCredits
from app.domain.enums import CreditLedgerKind
from app.domain.models.job import Job
from app.domain.repositories.credits import CreditsRepository
from app.domain.repositories.pricing import PricingRepository

logger = logging.getLogger(__name__)


@dataclass
class WalletView:
    available: int
    reserved: int


class CoinWalletService:
    """Единый кошелёк монет: reserve → capture → release по цене из прайс-листа.

    Все операции над строкой `coin_wallets` идут под `SELECT ... FOR UPDATE`
    (атомарность резерва). capture/release идемпотентны по `credit_ledger.idempotency_key`
    и зависят только от `job.reserved_credits` (монеты), не от `credit_category`.
    """

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def price_of(self, job_type: str) -> int:
        """Цена типа генерации в монетах (0 — бесплатно/неизвестно)."""
        async with self._sessionmaker() as session:
            price = await PricingRepository(session).get_active_price(job_type)
        return int(price or 0)

    async def reserve(self, *, user_id: UUID, job_type: str) -> int:
        """Резервирует цену типа генерации в монетах. Возвращает зарезервированную цену.

        Бесплатный тип (цена 0) → резерв не выполняется, возвращает 0.
        """
        async with self._sessionmaker() as session:
            async with session.begin():
                price = await PricingRepository(session).get_active_price(job_type)
                price = int(price or 0)
                if price <= 0:
                    return 0
                repo = CreditsRepository(session)
                wallet = await repo.ensure_wallet(user_id)
                if wallet.coins_available < price:
                    raise InsufficientCredits(
                        details={
                            "required": price,
                            "available": int(wallet.coins_available),
                        }
                    )
                wallet.coins_available -= price
                wallet.coins_reserved += price
                await repo.append_ledger(
                    user_id=user_id,
                    kind=CreditLedgerKind.debit_reserve,
                    amount=-price,
                    reason="reserve",
                )
        return price

    async def capture(self, *, job: Job) -> int:
        """Подтверждает списание зарезервированных монет. Идемпотентно по job.

        Списывается полная зарезервированная цена `job.reserved_credits`. Зависит
        только от резерва (не от `credit_category`).
        """
        units = int(job.reserved_credits or 0)
        if units <= 0:
            return 0
        async with self._sessionmaker() as session:
            async with session.begin():
                repo = CreditsRepository(session)
                newly = await repo.append_ledger(
                    user_id=job.user_id,
                    kind=CreditLedgerKind.debit_capture,
                    amount=-units,
                    ref_type="job",
                    ref_id=str(job.id),
                    idempotency_key=f"capture:{job.id}",
                )
                if not newly:
                    return units
                wallet = await repo.get_wallet_for_update(job.user_id)
                if wallet is not None:
                    wallet.coins_reserved = max(0, wallet.coins_reserved - units)
        return units

    async def release(self, *, job: Job) -> None:
        """Возвращает зарезервированные монеты в available (refund). Идемпотентно по job."""
        units = int(job.reserved_credits or 0)
        if units <= 0:
            return
        async with self._sessionmaker() as session:
            async with session.begin():
                repo = CreditsRepository(session)
                newly = await repo.append_ledger(
                    user_id=job.user_id,
                    kind=CreditLedgerKind.credit_release,
                    amount=units,
                    ref_type="job",
                    ref_id=str(job.id),
                    idempotency_key=f"release:{job.id}",
                )
                if not newly:
                    return
                wallet = await repo.get_wallet_for_update(job.user_id)
                if wallet is not None:
                    wallet.coins_reserved = max(0, wallet.coins_reserved - units)
                    wallet.coins_available += units

    async def wallet(self, *, user_id: UUID) -> WalletView:
        async with self._sessionmaker() as session:
            row = await CreditsRepository(session).get_wallet(user_id)
        if row is None:
            return WalletView(available=0, reserved=0)
        return WalletView(available=int(row.coins_available), reserved=int(row.coins_reserved))
