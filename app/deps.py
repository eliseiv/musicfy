from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.errors import AuthError, Forbidden, InvalidSession
from app.auth.sessions import AuthService
from app.config import Settings, get_settings
from app.domain.models.user import User

bearer_scheme = HTTPBearer(auto_error=False, description="Session token (Bearer)")
admin_scheme = HTTPBearer(auto_error=False, description="Админ-ключ (ADMIN_API_KEY)")


def get_settings_dep() -> Settings:
    return get_settings()


def get_sessionmaker(request: Request) -> async_sessionmaker[AsyncSession]:
    sm = getattr(request.app.state, "sessionmaker", None)
    if sm is None:
        raise RuntimeError("Sessionmaker is not configured")
    return sm


def get_auth_service(request: Request) -> AuthService:
    svc = getattr(request.app.state, "auth_service", None)
    if not isinstance(svc, AuthService):
        raise RuntimeError("AuthService is not configured")
    return svc


def get_fal_provider(request: Request):
    fal = getattr(request.app.state, "fal_provider", None)
    if fal is None:
        from app.api.errors import FalProviderError

        raise FalProviderError(
            "fal provider is not configured", code="PROVIDER_UNAVAILABLE", http_status=503
        )
    return fal


def get_generation_service(request: Request):
    svc = getattr(request.app.state, "generation_service", None)
    if svc is None:
        from app.api.errors import FalProviderError

        raise FalProviderError(
            "generation is not configured", code="PROVIDER_UNAVAILABLE", http_status=503
        )
    return svc


def get_lyrics_service(request: Request):
    svc = getattr(request.app.state, "lyrics_service", None)
    if svc is None:
        from app.api.errors import FalProviderError

        raise FalProviderError(
            "lyrics is not configured", code="PROVIDER_UNAVAILABLE", http_status=503
        )
    return svc


def get_pipeline_runner(request: Request):
    runner = getattr(request.app.state, "pipeline_runner", None)
    if runner is None:
        from app.api.errors import FalProviderError

        raise FalProviderError(
            "pipeline is not configured", code="PROVIDER_UNAVAILABLE", http_status=503
        )
    return runner


def get_credits_service(request: Request):
    svc = getattr(request.app.state, "credits_service", None)
    if svc is None:
        raise RuntimeError("Credits service is not configured")
    return svc


def get_billing_service(request: Request):
    svc = getattr(request.app.state, "billing_service", None)
    if svc is None:
        raise RuntimeError("Billing service is not configured")
    return svc


def get_asset_service(request: Request):
    svc = getattr(request.app.state, "asset_service", None)
    if svc is None:
        from app.api.errors import FalProviderError

        raise FalProviderError(
            "uploads are not configured", code="PROVIDER_UNAVAILABLE", http_status=503
        )
    return svc


def get_analytics_service(request: Request):
    svc = getattr(request.app.state, "analytics_service", None)
    if svc is None:
        raise RuntimeError("Analytics service is not configured")
    return svc


def get_admin_service(request: Request):
    svc = getattr(request.app.state, "admin_service", None)
    if svc is None:
        raise RuntimeError("Admin service is not configured")
    return svc


def require_admin(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Security(admin_scheme)
    ] = None,
) -> None:
    """Доступ к админ-эндпоинтам по выделенному ключу (Bearer = ADMIN_API_KEY)."""
    settings: Settings = request.app.state.settings
    token = credentials.credentials.strip() if credentials else None
    if (
        not settings.ADMIN_API_KEY
        or not token
        or not hmac.compare_digest(token, settings.ADMIN_API_KEY)
    ):
        raise Forbidden(message="Admin access required")


async def get_current_user(
    request: Request,
    auth: Annotated[AuthService, Depends(get_auth_service)],
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Security(bearer_scheme)
    ] = None,
) -> User:
    cached = getattr(request.state, "current_user", None)
    if isinstance(cached, User):
        return cached
    token = credentials.credentials.strip() if credentials else None
    if not token:
        raise AuthError()
    user = await auth.resolve(token)
    request.state.current_user = user
    request.state.user_id = user.id
    return user


async def get_optional_user(
    request: Request,
    auth: Annotated[AuthService, Depends(get_auth_service)],
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Security(bearer_scheme)
    ] = None,
) -> User | None:
    token = credentials.credentials.strip() if credentials else None
    if not token:
        return None
    try:
        user = await auth.resolve(token)
    except InvalidSession:
        return None
    request.state.current_user = user
    request.state.user_id = user.id
    return user
