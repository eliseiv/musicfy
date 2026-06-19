from __future__ import annotations

from pydantic import Field

from app.schemas.common import CamelModel


class GrantCreditsRequest(CamelModel):
    """Начислить покупные (non-expiring) кредиты в указанную категорию."""

    category: str = Field(description="song | cover | video")
    amount: int = Field(ge=1, le=100000, description="Сколько кредитов начислить.")
    reason: str | None = Field(default=None, max_length=255, description="Причина (для журнала).")


class GrantSubscriptionRequest(CamelModel):
    """Активировать подписку и выдать периодные лимиты по категориям."""

    song: int = Field(default=0, ge=0, le=100000, description="Лимит генераций песен на период.")
    cover: int = Field(default=0, ge=0, le=100000, description="Лимит каверов на период.")
    video: int = Field(default=0, ge=0, le=100000, description="Лимит видео на период.")
    period_days: int | None = Field(
        default=30, ge=1, le=3650, description="Длительность периода в днях (null = без срока)."
    )
    label: str = Field(default="admin_grant", max_length=255, description="Метка продукта/гранта.")


class AdminCategoryBalance(CamelModel):
    category: str
    subscription_remaining: int
    subscription_granted: int
    purchased_available: int


class AdminBalanceResponse(CamelModel):
    user_id: str
    balances: list[AdminCategoryBalance]
