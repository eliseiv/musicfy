from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.errors import InsufficientCredits
from app.domain.enums import CreditCategory, CreditLedgerKind, CreditSource
from app.domain.models.job import Job
from app.domain.repositories.credits import CreditsRepository

logger = logging.getLogger(__name__)


@dataclass
class BalanceView:
    category: CreditCategory
    subscription_remaining: int
    subscription_granted: int
    period_end: datetime | None
    purchased_available: int


class EntitlementService:
    """Списание генераций: сначала подписочный entitlement, затем покупные кредиты.

    Реализует протокол CreditGate (reserve/capture/release). В V1 единица = одна
    генерация (units обычно 1).
    """

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    @staticmethod
    def _entitlement_active(ent, now: datetime) -> bool:
        if ent is None or ent.granted - ent.used <= 0:
            return False
        if ent.period_end is not None and ent.period_end <= now:
            return False
        return True

    async def reserve(
        self, *, user_id: UUID, category: CreditCategory, units: int = 1
    ) -> CreditSource:
        now = datetime.now(UTC)
        async with self._sessionmaker() as session:
            async with session.begin():
                repo = CreditsRepository(session)
                ent = await repo.get_entitlement_for_update(
                    user_id=user_id, category=category
                )
                if self._entitlement_active(ent, now) and (ent.granted - ent.used) >= units:
                    ent.used += units
                    await repo.append_ledger(
                        user_id=user_id, kind=CreditLedgerKind.debit_reserve,
                        amount=-units, category=category, source=CreditSource.subscription,
                        reason="reserve",
                    )
                    return CreditSource.subscription

                bal = await repo.get_balance_for_update(user_id=user_id, category=category)
                if bal is not None and bal.available >= units:
                    bal.available -= units
                    bal.reserved += units
                    await repo.append_ledger(
                        user_id=user_id, kind=CreditLedgerKind.debit_reserve,
                        amount=-units, category=category, source=CreditSource.purchase,
                        reason="reserve",
                    )
                    return CreditSource.purchase

        raise InsufficientCredits(details={"category": category.value, "required": units})

    async def capture(self, *, job: Job, used_units: int) -> int:
        """Подтверждает списание зарезервированных единиц. Идемпотентно по job."""
        units = int(job.reserved_credits or 0)
        if units <= 0 or job.credit_category is None:
            return 0
        source = self._source_of(job)
        async with self._sessionmaker() as session:
            async with session.begin():
                repo = CreditsRepository(session)
                newly = await repo.append_ledger(
                    user_id=job.user_id, kind=CreditLedgerKind.debit_capture,
                    amount=-units, category=job.credit_category, source=source,
                    ref_type="job", ref_id=str(job.id),
                    idempotency_key=f"capture:{job.id}",
                )
                if not newly:
                    return units
                if source == CreditSource.purchase:
                    bal = await repo.get_balance_for_update(
                        user_id=job.user_id, category=job.credit_category
                    )
                    if bal is not None:
                        bal.reserved = max(0, bal.reserved - units)
        return units

    async def release(self, *, job: Job) -> None:
        units = int(job.reserved_credits or 0)
        if units <= 0 or job.credit_category is None:
            return
        source = self._source_of(job)
        async with self._sessionmaker() as session:
            async with session.begin():
                repo = CreditsRepository(session)
                newly = await repo.append_ledger(
                    user_id=job.user_id, kind=CreditLedgerKind.credit_release,
                    amount=units, category=job.credit_category, source=source,
                    ref_type="job", ref_id=str(job.id),
                    idempotency_key=f"release:{job.id}",
                )
                if not newly:
                    return
                if source == CreditSource.subscription:
                    ent = await repo.get_entitlement_for_update(
                        user_id=job.user_id, category=job.credit_category
                    )
                    if ent is not None:
                        ent.used = max(0, ent.used - units)
                else:
                    bal = await repo.get_balance_for_update(
                        user_id=job.user_id, category=job.credit_category
                    )
                    if bal is not None:
                        bal.reserved = max(0, bal.reserved - units)
                        bal.available += units

    @staticmethod
    def _source_of(job: Job) -> CreditSource:
        raw = (job.input_payload or {}).get("_credit_source")
        try:
            return CreditSource(raw)
        except ValueError:
            return CreditSource.purchase

    async def balances(self, *, user_id: UUID) -> list[BalanceView]:
        now = datetime.now(UTC)
        async with self._sessionmaker() as session:
            repo = CreditsRepository(session)
            ents = {e.category: e for e in await repo.list_entitlements(user_id)}
            bals = {b.category: b for b in await repo.list_balances(user_id)}
        views: list[BalanceView] = []
        for category in CreditCategory:
            ent = ents.get(category)
            bal = bals.get(category)
            remaining = 0
            granted = 0
            period_end = None
            if ent is not None:
                granted = ent.granted
                period_end = ent.period_end
                if self._entitlement_active(ent, now):
                    remaining = max(0, ent.granted - ent.used)
            views.append(
                BalanceView(
                    category=category,
                    subscription_remaining=remaining,
                    subscription_granted=granted,
                    period_end=period_end,
                    purchased_available=bal.available if bal else 0,
                )
            )
        return views
