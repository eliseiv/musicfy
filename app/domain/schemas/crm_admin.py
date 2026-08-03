"""Схемы CRM Admin API (универсальный контракт v1, snake_case JSON)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CrmUserListItem(BaseModel):
    id: str
    external_id: str | None = None
    is_paid: bool
    payments_count: int
    renewals_count: int
    tokens: int
    subscription_active: bool
    subscription_expires_at: str | None = None
    plan_id: str | None = None
    registered_at: str


class CrmUserListResponse(BaseModel):
    total: int
    items: list[CrmUserListItem]


class CrmBalanceBlock(BaseModel):
    tokens: int
    credited_total: int | None = None
    spent_total: int | None = None


class CrmSubscriptionBlock(BaseModel):
    plan_id: str | None = None
    plan_name: str | None = None
    price: str | None = None
    active: bool
    expires_at: str | None = None
    last_payment_at: str | None = None
    last_payment_method: str | None = None


class CrmMediaCountBlock(BaseModel):
    total: int
    success: int
    failed: int


class CrmAvgGenerationSec(BaseModel):
    photo: float | None = None
    video: float | None = None
    overall: float | None = None


class CrmMediaStats(BaseModel):
    photos: CrmMediaCountBlock
    videos: CrmMediaCountBlock
    avg_generation_sec: CrmAvgGenerationSec


class CrmUserDetailResponse(BaseModel):
    id: str
    external_id: str | None = None
    registered_at: str
    balance: CrmBalanceBlock
    subscription: CrmSubscriptionBlock
    revenue: None = None
    media_stats: CrmMediaStats | None = None


class CrmPaymentItem(BaseModel):
    title: str
    description: str | None = None
    amount: float
    currency: str
    status: str
    occurred_at: str


class CrmPaymentListResponse(BaseModel):
    total: int
    items: list[CrmPaymentItem]


class CrmRequestItem(BaseModel):
    endpoint: str
    prompt_preview: str | None = None
    status_code: int
    status: str
    duration_sec: float | None = None
    sent_at: str


class CrmRequestListResponse(BaseModel):
    total: int
    items: list[CrmRequestItem]


class CrmStatsResponse(BaseModel):
    users_total: int
    paid_users: int
    payments_sum_usd: float


class CrmProductItem(BaseModel):
    product_id: str
    name: str
    price: str | None = None
    period: str | None = None


class CrmProductListResponse(BaseModel):
    items: list[CrmProductItem]


class CrmAdjustTokensRequest(BaseModel):
    amount: int


class CrmAdjustTokensResponse(BaseModel):
    id: str
    tokens: int


class CrmGrantSubscriptionRequest(BaseModel):
    product_id: str = Field(min_length=1, max_length=255)
    expires_in_days: int = Field(gt=0, le=3650)
    grant_id: str = Field(min_length=1, max_length=255)


class CrmGrantSubscriptionResponse(BaseModel):
    id: str
    tokens: int
    subscription_active: bool
    subscription_expires_at: str | None = None
    applied: bool
