from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.schemas.common import CamelModel


class GuestSignInRequest(CamelModel):
    device_id: str | None = Field(
        default=None, max_length=255, description="Стабильный ID устройства (опционально)."
    )


class AppleSignInRequest(CamelModel):
    identity_token: str = Field(
        min_length=1, description="identityToken из ASAuthorizationAppleIDCredential."
    )
    nonce: str | None = Field(default=None, description="Nonce, переданный в запросе Apple (если был).")
    display_name: str | None = Field(default=None, max_length=120)


class SessionResponse(CamelModel):
    """Сессия. Используйте `token` как `Authorization: Bearer <token>`."""

    user_id: str
    is_guest: bool
    display_name: str | None
    token: str = Field(description="Bearer-токен сессии.")
    expires_at: datetime


class MeResponse(CamelModel):
    user_id: str
    is_guest: bool
    display_name: str | None
