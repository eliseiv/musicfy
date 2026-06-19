from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from app.api.errors import ValidationFailed
from app.deps import get_admin_service, get_credits_service, require_admin
from app.domain.enums import CreditCategory
from app.domain.schemas.admin import (
    AdminBalanceResponse,
    AdminCategoryBalance,
    GrantCreditsRequest,
    GrantSubscriptionRequest,
)
from app.domain.services.admin_service import AdminService
from app.domain.services.credits import EntitlementService

router = APIRouter(
    prefix="/admin",
    tags=["Админ"],
    dependencies=[Depends(require_admin)],
)


async def _balance_response(
    credits: EntitlementService, user_id: UUID
) -> AdminBalanceResponse:
    views = await credits.balances(user_id=user_id)
    return AdminBalanceResponse(
        user_id=str(user_id),
        balances=[
            AdminCategoryBalance(
                category=v.category.value,
                subscription_remaining=v.subscription_remaining,
                subscription_granted=v.subscription_granted,
                purchased_available=v.purchased_available,
            )
            for v in views
        ],
    )


def _parse_category(value: str) -> CreditCategory:
    try:
        return CreditCategory(value)
    except ValueError as exc:
        raise ValidationFailed(
            details={"field": "category", "allowed": ["song", "cover", "video"]}
        ) from exc


@router.get(
    "/users/{user_id}/balance",
    response_model=AdminBalanceResponse,
    summary="Баланс пользователя",
)
async def admin_balance(
    user_id: UUID,
    credits: Annotated[EntitlementService, Depends(get_credits_service)],
) -> AdminBalanceResponse:
    return await _balance_response(credits, user_id)


@router.post(
    "/users/{user_id}/credits",
    response_model=AdminBalanceResponse,
    summary="Начислить кредиты (пак)",
)
async def admin_grant_credits(
    user_id: UUID,
    body: GrantCreditsRequest,
    admin: Annotated[AdminService, Depends(get_admin_service)],
    credits: Annotated[EntitlementService, Depends(get_credits_service)],
) -> AdminBalanceResponse:
    await admin.grant_credits(
        user_id=user_id,
        category=_parse_category(body.category),
        amount=body.amount,
        reason=body.reason,
    )
    return await _balance_response(credits, user_id)


@router.post(
    "/users/{user_id}/subscription",
    response_model=AdminBalanceResponse,
    summary="Выдать подписку",
)
async def admin_grant_subscription(
    user_id: UUID,
    body: GrantSubscriptionRequest,
    admin: Annotated[AdminService, Depends(get_admin_service)],
    credits: Annotated[EntitlementService, Depends(get_credits_service)],
) -> AdminBalanceResponse:
    grants = {
        CreditCategory.song: body.song,
        CreditCategory.cover: body.cover,
        CreditCategory.video: body.video,
    }
    grants = {k: v for k, v in grants.items() if v > 0}
    if not grants:
        raise ValidationFailed(
            details={"reason": "no_grants", "hint": "укажите song/cover/video > 0"}
        )
    await admin.grant_subscription(
        user_id=user_id, grants=grants, period_days=body.period_days, label=body.label
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
    credits: Annotated[EntitlementService, Depends(get_credits_service)],
) -> AdminBalanceResponse:
    await admin.revoke_subscription(user_id=user_id)
    return await _balance_response(credits, user_id)
