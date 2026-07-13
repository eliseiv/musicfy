from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.deps import (
    get_billing_service,
    get_credits_service,
    get_current_user,
    get_sessionmaker,
)
from app.domain.models.user import User
from app.domain.repositories.credits import CreditsRepository
from app.domain.repositories.pricing import PricingRepository
from app.domain.repositories.products import ProductsRepository
from app.domain.schemas.billing import (
    ApplyResultResponse,
    BalanceResponse,
    LedgerEntryView,
    PriceView,
    PricingResponse,
    ProductView,
    RestoreRequest,
    VerifyPurchaseRequest,
)
from app.domain.services.billing_service import BillingService
from app.domain.services.credits import CoinWalletService

router = APIRouter(prefix="/billing", tags=["Биллинг"])


def _apply_result(result: dict) -> ApplyResultResponse:
    """Ответ применения транзакции. `reason` не теряется: status=rejected/ignored — не успех."""
    return ApplyResultResponse(
        status=result["status"],
        deduplicated=result.get("deduplicated", False),
        reason=result.get("reason"),
    )


@router.get("/balance", response_model=BalanceResponse, summary="Баланс монет")
async def balance(
    current: Annotated[User, Depends(get_current_user)],
    credits: Annotated[CoinWalletService, Depends(get_credits_service)],
) -> BalanceResponse:
    view = await credits.wallet(user_id=current.id)
    return BalanceResponse(
        coins_available=view.available,
        coins_reserved=view.reserved,
    )


@router.get("/pricing", response_model=PricingResponse, summary="Прайс-лист генераций")
async def pricing(sessionmaker: Annotated[object, Depends(get_sessionmaker)]) -> PricingResponse:
    async with sessionmaker() as session:
        rows = await PricingRepository(session).list_active()
        return PricingResponse(
            prices=[
                PriceView(job_type=r.job_type, price_coins=r.price_coins) for r in rows
            ]
        )


@router.get("/products", response_model=list[ProductView], summary="Каталог продуктов")
async def products(sessionmaker: Annotated[object, Depends(get_sessionmaker)]):
    async with sessionmaker() as session:
        rows = await ProductsRepository(session).list_active()
        return [
            ProductView(
                product_id=p.external_product_id,
                kind=p.kind.value,
                title=p.title,
                grants=p.grants or {},
                period_days=p.period_days,
            )
            for p in rows
        ]


@router.post("/purchases/verify", response_model=ApplyResultResponse, summary="Проверить покупку")
async def verify_purchase(
    body: VerifyPurchaseRequest,
    current: Annotated[User, Depends(get_current_user)],
    billing: Annotated[BillingService, Depends(get_billing_service)],
) -> ApplyResultResponse:
    result = await billing.verify_and_apply_transaction(
        user_id=current.id, signed_transaction=body.signed_transaction
    )
    return _apply_result(result)


@router.post("/restore", response_model=list[ApplyResultResponse], summary="Restore purchases")
async def restore(
    body: RestoreRequest,
    current: Annotated[User, Depends(get_current_user)],
    billing: Annotated[BillingService, Depends(get_billing_service)],
) -> list[ApplyResultResponse]:
    results = []
    for signed in body.signed_transactions:
        r = await billing.verify_and_apply_transaction(
            user_id=current.id, signed_transaction=signed
        )
        results.append(_apply_result(r))
    return results


@router.get("/ledger", response_model=list[LedgerEntryView], summary="Журнал кредитов")
async def ledger(
    current: Annotated[User, Depends(get_current_user)],
    sessionmaker: Annotated[object, Depends(get_sessionmaker)],
    limit: int = 100,
    offset: int = 0,
) -> list[LedgerEntryView]:
    async with sessionmaker() as session:
        rows = await CreditsRepository(session).list_ledger(
            user_id=current.id, limit=min(limit, 200), offset=offset
        )
        return [
            LedgerEntryView(
                kind=r.kind.value,
                category=r.category.value if r.category else None,
                source=r.source.value if r.source else None,
                amount=r.amount,
                reason=r.reason,
                created_at=r.created_at,
            )
            for r in rows
        ]
