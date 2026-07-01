from __future__ import annotations

from pydantic import Field

from app.schemas.common import CamelModel


class GrantCreditsRequest(CamelModel):
    """Начислить монеты в кошелёк пользователя (non-expiring)."""

    coins: int = Field(ge=1, le=1000000, description="Сколько монет начислить.")
    reason: str | None = Field(default=None, max_length=255, description="Причина (для журнала).")


class GrantSubscriptionRequest(CamelModel):
    """Активировать подписку и начислить монеты за период."""

    coins: int = Field(default=0, ge=0, le=1000000, description="Сколько монет начислить.")
    period_days: int | None = Field(
        default=30, ge=1, le=3650, description="Длительность периода в днях (null = без срока)."
    )
    label: str = Field(default="admin_grant", max_length=255, description="Метка продукта/гранта.")


class SetPriceRequest(CamelModel):
    """Обновить цену типа генерации в монетах."""

    price_coins: int = Field(ge=0, le=1000000, description="Цена типа генерации в монетах.")
    active: bool = Field(default=True, description="Активна ли строка прайс-листа.")


class AdminBalanceResponse(CamelModel):
    user_id: str
    coins_available: int
    coins_reserved: int


class AdminPriceResponse(CamelModel):
    job_type: str
    price_coins: int
    active: bool
