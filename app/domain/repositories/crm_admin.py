from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import String, and_, case, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import AuthProvider, JobStatus, JobType, SubscriptionStatus
from app.domain.models.auth_identity import AuthIdentity
from app.domain.models.billing import (
    CoinWallet,
    CreditLedgerEntry,
    Product,
    Purchase,
    SubscriptionState,
)
from app.domain.models.job import Job
from app.domain.models.user import User


def _utc_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class CrmUserRow:
    id: UUID
    external_id: str | None
    registered_at: datetime
    tokens: int
    payments_count: int
    renewals_count: int
    subscription_active: bool
    subscription_expires_at: datetime | None
    plan_id: str | None


class CrmAdminRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_user(self, user_id: UUID) -> User | None:
        return await self._session.get(User, user_id)

    async def get_guest_external_id(self, user_id: UUID) -> str | None:
        stmt = (
            select(AuthIdentity.subject)
            .where(
                AuthIdentity.user_id == user_id,
                AuthIdentity.provider == AuthProvider.guest,
            )
            .order_by(AuthIdentity.created_at.asc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_users(
        self,
        *,
        limit: int,
        offset: int,
        search: str | None,
        date_from: datetime | None,
        date_to: datetime | None,
        is_paid: bool | None,
    ) -> tuple[int, list[CrmUserRow]]:
        payments_sq = (
            select(
                Purchase.user_id.label("user_id"),
                func.count(Purchase.id).label("payments_count"),
                func.count(
                    case(
                        (
                            Purchase.transaction_id != Purchase.original_transaction_id,
                            Purchase.id,
                        ),
                        else_=None,
                    )
                ).label("renewals_count"),
            )
            .where(Purchase.status == "applied")
            .group_by(Purchase.user_id)
            .subquery()
        )
        guest_sq = (
            select(
                AuthIdentity.user_id.label("user_id"),
                func.min(AuthIdentity.subject).label("external_id"),
            )
            .where(AuthIdentity.provider == AuthProvider.guest)
            .group_by(AuthIdentity.user_id)
            .subquery()
        )

        filters: list[Any] = []
        if search:
            pattern = f"%{search.strip()}%"
            filters.append(
                or_(
                    cast(User.id, String).ilike(pattern),
                    guest_sq.c.external_id.ilike(pattern),
                )
            )
        if date_from is not None:
            filters.append(User.created_at >= date_from)
        if date_to is not None:
            filters.append(User.created_at <= date_to)

        payments_count_col = func.coalesce(payments_sq.c.payments_count, 0)
        if is_paid is True:
            filters.append(payments_count_col > 0)
        elif is_paid is False:
            filters.append(payments_count_col == 0)

        base = (
            select(
                User.id,
                guest_sq.c.external_id,
                User.created_at,
                func.coalesce(CoinWallet.coins_available, 0).label("tokens"),
                payments_count_col.label("payments_count"),
                func.coalesce(payments_sq.c.renewals_count, 0).label("renewals_count"),
                SubscriptionState.status,
                SubscriptionState.expires_at,
                SubscriptionState.product_external_id,
            )
            .outerjoin(guest_sq, guest_sq.c.user_id == User.id)
            .outerjoin(CoinWallet, CoinWallet.user_id == User.id)
            .outerjoin(payments_sq, payments_sq.c.user_id == User.id)
            .outerjoin(SubscriptionState, SubscriptionState.user_id == User.id)
        )
        if filters:
            base = base.where(and_(*filters))

        count_stmt = select(func.count()).select_from(base.subquery())
        total = int((await self._session.execute(count_stmt)).scalar_one())

        rows_stmt = (
            base.order_by(User.created_at.desc()).limit(limit).offset(offset)
        )
        rows = (await self._session.execute(rows_stmt)).all()

        items = [
            CrmUserRow(
                id=row.id,
                external_id=row.external_id,
                registered_at=row.created_at,
                tokens=int(row.tokens),
                payments_count=int(row.payments_count),
                renewals_count=int(row.renewals_count),
                subscription_active=row.status == SubscriptionStatus.active,
                subscription_expires_at=row.expires_at,
                plan_id=row.product_external_id,
            )
            for row in rows
        ]
        return total, items

    async def ledger_totals(self, user_id: UUID) -> tuple[int, int]:
        credited_kinds = (
            "credit_subscription_grant",
            "credit_purchase",
            "credit_promo",
            "credit_release",
            "credit_refund",
            "credit_adjustment",
        )
        spent_kinds = ("debit_capture", "debit_adjustment", "debit_expire")

        credited_stmt = select(func.coalesce(func.sum(CreditLedgerEntry.amount), 0)).where(
            CreditLedgerEntry.user_id == user_id,
            CreditLedgerEntry.kind.in_(credited_kinds),
            CreditLedgerEntry.amount > 0,
        )
        spent_stmt = select(func.coalesce(func.sum(CreditLedgerEntry.amount), 0)).where(
            CreditLedgerEntry.user_id == user_id,
            CreditLedgerEntry.kind.in_(spent_kinds),
            CreditLedgerEntry.amount > 0,
        )
        credited = int((await self._session.execute(credited_stmt)).scalar_one())
        spent = int((await self._session.execute(spent_stmt)).scalar_one())
        return credited, spent

    async def get_subscription(self, user_id: UUID) -> SubscriptionState | None:
        stmt = select(SubscriptionState).where(SubscriptionState.user_id == user_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_product(self, external_id: str) -> Product | None:
        stmt = select(Product).where(Product.external_product_id == external_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_active_products(self) -> list[Product]:
        stmt = (
            select(Product)
            .where(Product.active.is_(True))
            .order_by(Product.kind.asc(), Product.external_product_id.asc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def count_payments(self, user_id: UUID) -> int:
        stmt = select(func.count(Purchase.id)).where(
            Purchase.user_id == user_id, Purchase.status == "applied"
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def list_payments(
        self, *, user_id: UUID, limit: int, offset: int
    ) -> tuple[int, list[Purchase]]:
        count_stmt = select(func.count(Purchase.id)).where(
            Purchase.user_id == user_id, Purchase.status == "applied"
        )
        total = int((await self._session.execute(count_stmt)).scalar_one())
        stmt = (
            select(Purchase)
            .where(Purchase.user_id == user_id, Purchase.status == "applied")
            .order_by(Purchase.purchase_date.desc().nullslast(), Purchase.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        items = list((await self._session.execute(stmt)).scalars().all())
        return total, items

    async def count_jobs(self, user_id: UUID) -> int:
        stmt = select(func.count(Job.id)).where(Job.user_id == user_id)
        return int((await self._session.execute(stmt)).scalar_one())

    async def list_jobs(
        self, *, user_id: UUID, limit: int, offset: int
    ) -> tuple[int, list[Job]]:
        count_stmt = select(func.count(Job.id)).where(Job.user_id == user_id)
        total = int((await self._session.execute(count_stmt)).scalar_one())
        stmt = (
            select(Job)
            .where(Job.user_id == user_id)
            .order_by(Job.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        items = list((await self._session.execute(stmt)).scalars().all())
        return total, items

    async def job_media_stats(self, user_id: UUID) -> dict[str, Any]:
        photo_types = (JobType.song, JobType.cover, JobType.lyrics, JobType.voice_clone)
        video_type = JobType.video

        async def _counts(job_types: tuple[JobType, ...]) -> tuple[int, int, int]:
            row = (
                await self._session.execute(
                    select(
                        func.count(Job.id),
                        func.count(
                            case((Job.status == JobStatus.completed, Job.id), else_=None)
                        ),
                        func.count(
                            case((Job.status == JobStatus.failed, Job.id), else_=None)
                        ),
                    ).where(Job.user_id == user_id, Job.job_type.in_(job_types))
                )
            ).one()
            return int(row[0]), int(row[1]), int(row[2])

        async def _avg(job_types: tuple[JobType, ...] | None = None) -> float | None:
            stmt = select(
                func.avg(
                    func.extract("epoch", Job.finished_at - Job.started_at)
                )
            ).where(
                Job.user_id == user_id,
                Job.status == JobStatus.completed,
                Job.started_at.is_not(None),
                Job.finished_at.is_not(None),
            )
            if job_types is not None:
                stmt = stmt.where(Job.job_type.in_(job_types))
            value = (await self._session.execute(stmt)).scalar_one()
            return float(value) if value is not None else None

        photo_total, photo_ok, photo_fail = await _counts(photo_types)
        video_total, video_ok, video_fail = await _counts((video_type,))

        return {
            "photos": (photo_total, photo_ok, photo_fail),
            "videos": (video_total, video_ok, video_fail),
            "avg_photo": await _avg(photo_types),
            "avg_video": await _avg((video_type,)),
            "avg_overall": await _avg(None),
        }

    async def global_stats(
        self,
        *,
        date_from: datetime | None,
        date_to: datetime | None,
    ) -> tuple[int, int, float]:
        user_filters: list[Any] = []
        if date_from is not None:
            user_filters.append(User.created_at >= date_from)
        if date_to is not None:
            user_filters.append(User.created_at <= date_to)

        users_stmt = select(func.count(User.id))
        if user_filters:
            users_stmt = users_stmt.where(and_(*user_filters))
        users_total = int((await self._session.execute(users_stmt)).scalar_one())

        paid_users_stmt = select(func.count(func.distinct(Purchase.user_id))).where(
            Purchase.status == "applied"
        )
        paid_users = int((await self._session.execute(paid_users_stmt)).scalar_one())

        purchase_filters = [Purchase.status == "applied"]
        if date_from is not None:
            purchase_filters.append(Purchase.purchase_date >= date_from)
        if date_to is not None:
            purchase_filters.append(Purchase.purchase_date <= date_to)

        purchases = (
            await self._session.execute(
                select(Purchase.raw).where(and_(*purchase_filters))
            )
        ).scalars().all()

        payments_sum = 0.0
        for raw in purchases:
            if not raw:
                continue
            currency = str(raw.get("currency") or "USD").upper()
            if currency != "USD":
                continue
            price = raw.get("price")
            if price is None:
                continue
            try:
                payments_sum += float(price) / 1000.0
            except (TypeError, ValueError):
                continue

        return users_total, paid_users, round(payments_sum, 2)

    async def find_product_title(self, external_id: str) -> str | None:
        product = await self.get_product(external_id)
        return product.title if product else None

    async def last_payment(self, user_id: UUID) -> Purchase | None:
        stmt = (
            select(Purchase)
            .where(Purchase.user_id == user_id, Purchase.status == "applied")
            .order_by(Purchase.purchase_date.desc().nullslast(), Purchase.created_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()
