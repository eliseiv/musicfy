"""CRM Admin API (универсальный контракт v1) под префиксом /v1/admin."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.deps import get_crm_admin_service, require_admin
from app.domain.schemas.crm_admin import (
    CrmAdjustTokensRequest,
    CrmAdjustTokensResponse,
    CrmGrantSubscriptionRequest,
    CrmGrantSubscriptionResponse,
    CrmPaymentListResponse,
    CrmProductListResponse,
    CrmRequestListResponse,
    CrmStatsResponse,
    CrmUserDetailResponse,
    CrmUserListResponse,
)
from app.domain.services.crm_admin_service import CrmAdminService

router = APIRouter(tags=["CRM Admin"], dependencies=[Depends(require_admin)])


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


@router.get("/users", response_model=CrmUserListResponse, summary="CRM: список пользователей")
async def crm_list_users(
    crm: Annotated[CrmAdminService, Depends(get_crm_admin_service)],
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    search: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    is_paid: bool | None = Query(default=None),
) -> CrmUserListResponse:
    return await crm.list_users(
        limit=limit,
        offset=offset,
        search=search,
        date_from=_parse_dt(date_from),
        date_to=_parse_dt(date_to),
        is_paid=is_paid,
    )


@router.get(
    "/users/{user_id}",
    response_model=CrmUserDetailResponse,
    summary="CRM: карточка пользователя",
)
async def crm_get_user(
    user_id: UUID,
    crm: Annotated[CrmAdminService, Depends(get_crm_admin_service)],
) -> CrmUserDetailResponse:
    return await crm.get_user(user_id)


@router.get(
    "/users/{user_id}/payments",
    response_model=CrmPaymentListResponse,
    summary="CRM: история оплат",
)
async def crm_user_payments(
    user_id: UUID,
    crm: Annotated[CrmAdminService, Depends(get_crm_admin_service)],
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> CrmPaymentListResponse:
    return await crm.list_payments(user_id=user_id, limit=limit, offset=offset)


@router.get(
    "/users/{user_id}/requests",
    response_model=CrmRequestListResponse,
    summary="CRM: история запросов",
)
async def crm_user_requests(
    user_id: UUID,
    crm: Annotated[CrmAdminService, Depends(get_crm_admin_service)],
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> CrmRequestListResponse:
    return await crm.list_requests(user_id=user_id, limit=limit, offset=offset)


@router.get("/stats", response_model=CrmStatsResponse, summary="CRM: сводка")
async def crm_stats(
    crm: Annotated[CrmAdminService, Depends(get_crm_admin_service)],
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
) -> CrmStatsResponse:
    return await crm.stats(date_from=_parse_dt(date_from), date_to=_parse_dt(date_to))


@router.get("/products", response_model=CrmProductListResponse, summary="CRM: тарифы")
async def crm_products(
    crm: Annotated[CrmAdminService, Depends(get_crm_admin_service)],
) -> CrmProductListResponse:
    return await crm.products()


@router.post(
    "/users/{user_id}/tokens",
    response_model=CrmAdjustTokensResponse,
    summary="CRM: начислить/списать токены",
)
async def crm_adjust_tokens(
    user_id: UUID,
    body: CrmAdjustTokensRequest,
    crm: Annotated[CrmAdminService, Depends(get_crm_admin_service)],
) -> CrmAdjustTokensResponse:
    return await crm.adjust_tokens(user_id=user_id, amount=body.amount)


@router.post(
    "/users/{user_id}/subscription",
    response_model=CrmGrantSubscriptionResponse,
    summary="CRM: выдать/продлить подписку",
)
async def crm_grant_subscription(
    user_id: UUID,
    body: CrmGrantSubscriptionRequest,
    crm: Annotated[CrmAdminService, Depends(get_crm_admin_service)],
) -> CrmGrantSubscriptionResponse:
    return await crm.grant_subscription(
        user_id=user_id,
        product_id=body.product_id,
        expires_in_days=body.expires_in_days,
        grant_id=body.grant_id,
    )
