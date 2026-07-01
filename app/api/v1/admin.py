from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from app.api.errors import ValidationFailed
from app.deps import get_admin_service, get_credits_service, require_admin
from app.domain.enums import JobType
from app.domain.schemas.admin import (
    AdminBalanceResponse,
    AdminPriceResponse,
    GrantCreditsRequest,
    GrantSubscriptionRequest,
    SetPriceRequest,
)
from app.domain.services.admin_service import AdminService
from app.domain.services.credits import CoinWalletService

router = APIRouter(
    prefix="/admin",
    tags=["Админ"],
    dependencies=[Depends(require_admin)],
)


async def _balance_response(
    credits: CoinWalletService, user_id: UUID
) -> AdminBalanceResponse:
    view = await credits.wallet(user_id=user_id)
    return AdminBalanceResponse(
        user_id=str(user_id),
        coins_available=view.available,
        coins_reserved=view.reserved,
    )


def _parse_job_type(value: str) -> str:
    try:
        return JobType(value).value
    except ValueError as exc:
        raise ValidationFailed(
            details={
                "field": "jobType",
                "allowed": [t.value for t in JobType],
            }
        ) from exc


@router.get(
    "/users/{user_id}/balance",
    response_model=AdminBalanceResponse,
    summary="Баланс монет пользователя",
)
async def admin_balance(
    user_id: UUID,
    credits: Annotated[CoinWalletService, Depends(get_credits_service)],
) -> AdminBalanceResponse:
    return await _balance_response(credits, user_id)


@router.post(
    "/users/{user_id}/credits",
    response_model=AdminBalanceResponse,
    summary="Начислить монеты",
)
async def admin_grant_credits(
    user_id: UUID,
    body: GrantCreditsRequest,
    admin: Annotated[AdminService, Depends(get_admin_service)],
    credits: Annotated[CoinWalletService, Depends(get_credits_service)],
) -> AdminBalanceResponse:
    await admin.grant_credits(user_id=user_id, coins=body.coins, reason=body.reason)
    return await _balance_response(credits, user_id)


@router.post(
    "/users/{user_id}/subscription",
    response_model=AdminBalanceResponse,
    summary="Выдать подписку (начислить монеты)",
)
async def admin_grant_subscription(
    user_id: UUID,
    body: GrantSubscriptionRequest,
    admin: Annotated[AdminService, Depends(get_admin_service)],
    credits: Annotated[CoinWalletService, Depends(get_credits_service)],
) -> AdminBalanceResponse:
    await admin.grant_subscription(
        user_id=user_id,
        coins=body.coins,
        period_days=body.period_days,
        label=body.label,
    )
    return await _balance_response(credits, user_id)


@router.delete(
    "/users/{user_id}/subscription",
    response_model=AdminBalanceResponse,
    summary="Отозвать подписку",
)
async def admin_revoke_subscription(
    user_id: UUID,
    admin: Annotated[AdminService, Depends(get_admin_service)],
    credits: Annotated[CoinWalletService, Depends(get_credits_service)],
) -> AdminBalanceResponse:
    await admin.revoke_subscription(user_id=user_id)
    return await _balance_response(credits, user_id)


@router.patch(
    "/pricing/{job_type}",
    response_model=AdminPriceResponse,
    summary="Изменить цену типа генерации",
)
async def admin_set_price(
    job_type: str,
    body: SetPriceRequest,
    admin: Annotated[AdminService, Depends(get_admin_service)],
) -> AdminPriceResponse:
    resolved = _parse_job_type(job_type)
    jt, price, active = await admin.set_price(
        job_type=resolved, price_coins=body.price_coins, active=body.active
    )
    return AdminPriceResponse(job_type=jt, price_coins=price, active=active)
