from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.enums import (
    BillingProvider,
    CreditLedgerKind,
    CreditSource,
    JobStatus,
    ProductKind,
    SubscriptionStatus,
)
from app.domain.repositories.credits import CreditsRepository
from app.domain.repositories.crm_admin import CrmAdminRepository, CrmUserRow, _utc_iso
from app.domain.schemas.crm_admin import (
    CrmAdjustTokensResponse,
    CrmAvgGenerationSec,
    CrmBalanceBlock,
    CrmGrantSubscriptionResponse,
    CrmMediaCountBlock,
    CrmMediaStats,
    CrmPaymentItem,
    CrmPaymentListResponse,
    CrmProductItem,
    CrmProductListResponse,
    CrmRequestItem,
    CrmRequestListResponse,
    CrmStatsResponse,
    CrmSubscriptionBlock,
    CrmUserDetailResponse,
    CrmUserListItem,
    CrmUserListResponse,
)

_SLOW_REQUEST_SEC = 30.0
_PRICE_HINT_RE = re.compile(r"(\d+(?:\.\d+)?)")


class CrmAdminService:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def list_users(
        self,
        *,
        limit: int,
        offset: int,
        search: str | None,
        date_from: datetime | None,
        date_to: datetime | None,
        is_paid: bool | None,
    ) -> CrmUserListResponse:
        limit = min(max(limit, 1), 100)
        offset = max(offset, 0)
        async with self._sessionmaker() as session:
            repo = CrmAdminRepository(session)
            total, rows = await repo.list_users(
                limit=limit,
                offset=offset,
                search=search,
                date_from=date_from,
                date_to=date_to,
                is_paid=is_paid,
            )
        return CrmUserListResponse(
            total=total,
            items=[self._user_list_item(row) for row in rows],
        )

    async def get_user(self, user_id: UUID) -> CrmUserDetailResponse:
        async with self._sessionmaker() as session:
            repo = CrmAdminRepository(session)
            user = await repo.get_user(user_id)
            if user is None:
                raise HTTPException(status_code=404, detail="User not found")

            external_id = await repo.get_guest_external_id(user_id)
            wallet = await CreditsRepository(session).get_wallet(user_id)
            tokens = int(wallet.coins_available) if wallet else 0
            credited, spent = await repo.ledger_totals(user_id)
            sub = await repo.get_subscription(user_id)
            product = (
                await repo.get_product(sub.product_external_id)
                if sub and sub.product_external_id
                else None
            )
            last_payment = await repo.last_payment(user_id)
            media = await repo.job_media_stats(user_id)

        return CrmUserDetailResponse(
            id=str(user_id),
            external_id=external_id,
            registered_at=_utc_iso(user.created_at) or "",
            balance=CrmBalanceBlock(
                tokens=tokens,
                credited_total=credited,
                spent_total=spent,
            ),
            subscription=self._subscription_block(sub, product, last_payment),
            revenue=None,
            media_stats=self._media_stats(media),
        )

    async def list_payments(
        self, *, user_id: UUID, limit: int, offset: int
    ) -> CrmPaymentListResponse:
        await self._ensure_user_exists(user_id)
        limit = min(max(limit, 1), 100)
        offset = max(offset, 0)
        async with self._sessionmaker() as session:
            repo = CrmAdminRepository(session)
            total, purchases = await repo.list_payments(
                user_id=user_id, limit=limit, offset=offset
            )
            items: list[CrmPaymentItem] = []
            for purchase in purchases:
                title = await repo.find_product_title(purchase.product_external_id)
                items.append(self._payment_item(purchase, title or purchase.product_external_id))
        return CrmPaymentListResponse(total=total, items=items)

    async def list_requests(
        self, *, user_id: UUID, limit: int, offset: int
    ) -> CrmRequestListResponse:
        await self._ensure_user_exists(user_id)
        limit = min(max(limit, 1), 100)
        offset = max(offset, 0)
        async with self._sessionmaker() as session:
            repo = CrmAdminRepository(session)
            total, jobs = await repo.list_jobs(user_id=user_id, limit=limit, offset=offset)
        return CrmRequestListResponse(
            total=total,
            items=[self._request_item(job) for job in jobs],
        )

    async def stats(
        self,
        *,
        date_from: datetime | None,
        date_to: datetime | None,
    ) -> CrmStatsResponse:
        async with self._sessionmaker() as session:
            repo = CrmAdminRepository(session)
            users_total, paid_users, payments_sum = await repo.global_stats(
                date_from=date_from, date_to=date_to
            )
        return CrmStatsResponse(
            users_total=users_total,
            paid_users=paid_users,
            payments_sum_usd=payments_sum,
        )

    async def products(self) -> CrmProductListResponse:
        async with self._sessionmaker() as session:
            repo = CrmAdminRepository(session)
            rows = await repo.list_active_products()
        return CrmProductListResponse(
            items=[self._product_item(product) for product in rows]
        )

    async def adjust_tokens(self, *, user_id: UUID, amount: int) -> CrmAdjustTokensResponse:
        if amount == 0:
            raise HTTPException(status_code=400, detail="amount must not be zero")

        async with self._sessionmaker() as session:
            async with session.begin():
                repo = CrmAdminRepository(session)
                if await repo.get_user(user_id) is None:
                    raise HTTPException(status_code=404, detail="User not found")

                credits = CreditsRepository(session)
                wallet = await credits.ensure_wallet(user_id)
                new_balance = int(wallet.coins_available) + amount
                if new_balance < 0:
                    raise HTTPException(status_code=400, detail="insufficient balance")

                wallet.coins_available = new_balance
                if amount > 0:
                    kind = CreditLedgerKind.credit_adjustment
                    ledger_amount = amount
                else:
                    kind = CreditLedgerKind.debit_adjustment
                    ledger_amount = abs(amount)

                await credits.append_ledger(
                    user_id=user_id,
                    kind=kind,
                    amount=ledger_amount,
                    source=CreditSource.promo,
                    reason="crm_admin_adjust",
                    ref_type="crm_admin",
                )

        return CrmAdjustTokensResponse(id=str(user_id), tokens=new_balance)

    async def grant_subscription(
        self,
        *,
        user_id: UUID,
        product_id: str,
        expires_in_days: int,
        grant_id: str,
    ) -> CrmGrantSubscriptionResponse:
        idempotency_key = f"crm:subscription:{grant_id}"

        async with self._sessionmaker() as session:
            async with session.begin():
                repo = CrmAdminRepository(session)
                if await repo.get_user(user_id) is None:
                    raise HTTPException(status_code=404, detail="User not found")

                product = await repo.get_product(product_id)
                if product is None or product.kind != ProductKind.subscription:
                    raise HTTPException(status_code=400, detail="unknown product")

                credits = CreditsRepository(session)
                if await credits.idempotency_exists(idempotency_key):
                    wallet = await credits.get_wallet(user_id)
                    sub = await repo.get_subscription(user_id)
                    return self._subscription_grant_response(
                        user_id=user_id,
                        wallet=wallet,
                        sub=sub,
                        applied=False,
                    )

                now = datetime.now(UTC)
                sub = await credits.get_subscription_for_update(user_id)
                base = now
                if (
                    sub is not None
                    and sub.status == SubscriptionStatus.active
                    and sub.expires_at is not None
                    and sub.expires_at > now
                ):
                    base = sub.expires_at
                expires_at = base + timedelta(days=expires_in_days)

                coins = int((product.grants or {}).get("coins") or 0)
                if coins > 0:
                    wallet = await credits.ensure_wallet(user_id)
                    wallet.coins_available += coins

                await credits.upsert_subscription(
                    user_id=user_id,
                    values={
                        "status": SubscriptionStatus.active,
                        "provider": BillingProvider.apple,
                        "product_external_id": product.external_product_id,
                        "original_transaction_id": f"crm:{grant_id}",
                        "expires_at": expires_at,
                    },
                )
                await credits.append_ledger(
                    user_id=user_id,
                    kind=CreditLedgerKind.credit_subscription_grant,
                    amount=coins,
                    source=CreditSource.subscription,
                    reason=f"crm_admin:{product_id}",
                    ref_type="crm_admin",
                    ref_id=grant_id,
                    idempotency_key=idempotency_key,
                )
                wallet = await credits.get_wallet(user_id)
                sub = await repo.get_subscription(user_id)

        return self._subscription_grant_response(
            user_id=user_id,
            wallet=wallet,
            sub=sub,
            applied=True,
        )

    async def _ensure_user_exists(self, user_id: UUID) -> None:
        async with self._sessionmaker() as session:
            repo = CrmAdminRepository(session)
            if await repo.get_user(user_id) is None:
                raise HTTPException(status_code=404, detail="User not found")

    @staticmethod
    def _user_list_item(row: CrmUserRow) -> CrmUserListItem:
        return CrmUserListItem(
            id=str(row.id),
            external_id=row.external_id,
            is_paid=row.payments_count > 0,
            payments_count=row.payments_count,
            renewals_count=row.renewals_count,
            tokens=row.tokens,
            subscription_active=row.subscription_active,
            subscription_expires_at=_utc_iso(row.subscription_expires_at),
            plan_id=row.plan_id,
            registered_at=_utc_iso(row.registered_at) or "",
        )

    @staticmethod
    def _subscription_block(sub, product, last_payment) -> CrmSubscriptionBlock:
        active = sub is not None and sub.status == SubscriptionStatus.active
        plan_id = sub.product_external_id if sub else None
        plan_name = product.title if product else None
        price = CrmAdminService._product_price_hint(plan_id) if plan_id else None
        last_at = None
        last_method = None
        if last_payment is not None:
            last_at = _utc_iso(last_payment.purchase_date or last_payment.created_at)
            env = last_payment.environment
            last_method = f"App Store ({env})" if env else "App Store"
        return CrmSubscriptionBlock(
            plan_id=plan_id,
            plan_name=plan_name,
            price=price,
            active=active,
            expires_at=_utc_iso(sub.expires_at) if sub else None,
            last_payment_at=last_at,
            last_payment_method=last_method,
        )

    @staticmethod
    def _media_stats(raw: dict) -> CrmMediaStats:
        p_total, p_ok, p_fail = raw["photos"]
        v_total, v_ok, v_fail = raw["videos"]
        return CrmMediaStats(
            photos=CrmMediaCountBlock(total=p_total, success=p_ok, failed=p_fail),
            videos=CrmMediaCountBlock(total=v_total, success=v_ok, failed=v_fail),
            avg_generation_sec=CrmAvgGenerationSec(
                photo=raw["avg_photo"],
                video=raw["avg_video"],
                overall=raw["avg_overall"],
            ),
        )

    @staticmethod
    def _payment_item(purchase, title: str) -> CrmPaymentItem:
        raw = purchase.raw or {}
        amount = 0.0
        if raw.get("price") is not None:
            try:
                amount = float(raw["price"]) / 1000.0
            except (TypeError, ValueError):
                amount = 0.0
        currency = str(raw.get("currency") or "USD").upper()
        is_renewal = (
            purchase.original_transaction_id is not None
            and purchase.transaction_id != purchase.original_transaction_id
        )
        description = None
        if is_renewal:
            description = "Автопродление"
        if purchase.environment:
            env = f" · {purchase.environment}"
            description = (description or "Покупка") + env
        return CrmPaymentItem(
            title=title,
            description=description,
            amount=amount,
            currency=currency,
            status="success" if purchase.status == "applied" else "failed",
            occurred_at=_utc_iso(purchase.purchase_date or purchase.created_at) or "",
        )

    @staticmethod
    def _request_item(job) -> CrmRequestItem:
        payload = job.input_payload or {}
        prompt = (
            payload.get("prompt")
            or payload.get("lyrics")
            or payload.get("text")
            or payload.get("title")
        )
        preview = None
        if isinstance(prompt, str) and prompt.strip():
            preview = prompt.strip()[:120]

        duration = None
        if job.started_at and job.finished_at:
            duration = max(
                (job.finished_at - job.started_at).total_seconds(),
                0.0,
            )

        if job.status == JobStatus.completed:
            status_code = 200
            status = "ok"
        elif job.status == JobStatus.failed:
            status_code = 500
            status = "error"
        else:
            status_code = 202
            status = "ok"

        if duration is not None and duration >= _SLOW_REQUEST_SEC and status == "ok":
            status = "slow"

        return CrmRequestItem(
            endpoint=job.job_type.value,
            prompt_preview=preview,
            status_code=status_code,
            status=status,
            duration_sec=duration,
            sent_at=_utc_iso(job.created_at) or "",
        )

    @staticmethod
    def _product_item(product) -> CrmProductItem:
        period = None
        if product.period_days:
            if product.period_days == 7:
                period = "week"
            elif product.period_days == 30:
                period = "month"
            elif product.period_days == 365:
                period = "year"
            else:
                period = f"{product.period_days}d"
        return CrmProductItem(
            product_id=product.external_product_id,
            name=product.title,
            price=CrmAdminService._product_price_hint(product.external_product_id),
            period=period,
        )

    @staticmethod
    def _product_price_hint(product_id: str | None) -> str | None:
        if not product_id:
            return None
        match = _PRICE_HINT_RE.search(product_id)
        if not match:
            return None
        return f"${match.group(1)}"

    @staticmethod
    def _subscription_grant_response(*, user_id, wallet, sub, applied: bool):
        tokens = int(wallet.coins_available) if wallet else 0
        active = sub is not None and sub.status == SubscriptionStatus.active
        return CrmGrantSubscriptionResponse(
            id=str(user_id),
            tokens=tokens,
            subscription_active=active,
            subscription_expires_at=_utc_iso(sub.expires_at) if sub else None,
            applied=applied,
        )
