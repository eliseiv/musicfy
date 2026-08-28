from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.errors import APIError, AuthError, Forbidden, InvalidSession
from app.auth.sessions import AuthService
from app.config import Settings, get_settings
from app.domain.models.user import User

bearer_scheme = HTTPBearer(auto_error=False, description="Session token (Bearer)")
admin_scheme = HTTPBearer(auto_error=False, description="Админ-ключ (ADMIN_API_KEY)")
adapty_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="adaptyWebhook",
    description=(
        "Статический bearer вебхука Adapty (`ADAPTY_WEBHOOK_SECRET`). Вызывает Adapty, не "
        "клиент. НЕ пользовательский токен сессии и НЕ админ-ключ."
    ),
)


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


def get_adapty_webhook_service(request: Request):
    svc = getattr(request.app.state, "adapty_webhook_service", None)
    if svc is None:
        raise RuntimeError("Adapty webhook service is not configured")
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


def get_crm_admin_service(request: Request):
    svc = getattr(request.app.state, "crm_admin_service", None)
    if svc is None:
        raise RuntimeError("CRM admin service is not configured")
    return svc


def require_admin(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Security(admin_scheme)
    ] = None,
) -> None:
    """Доступ к админ-эндпоинтам по X-Admin-Key (CRM) или Bearer = ADMIN_API_KEY (legacy)."""
    settings: Settings = request.app.state.settings
    if not settings.ADMIN_API_KEY:
        raise AuthError(message="Admin API key not configured")

    header_key = request.headers.get("X-Admin-Key")
    bearer_key = credentials.credentials.strip() if credentials else None
    token = (header_key or bearer_key or "").strip()

    if not token:
        raise Forbidden(message="Admin access required")

    if not hmac.compare_digest(token, settings.ADMIN_API_KEY):
        raise AuthError(message="Invalid admin API key")


def require_adapty_webhook(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Security(adapty_scheme)
    ] = None,
) -> None:
    """Авторизация вебхука Adapty статическим bearer-секретом (ADR-019).

    Adapty НЕ подписывает payload, поэтому HMAC по телу невозможен — подлинность вызова
    держится только на общем секрете, который оператор прописывает и в `.env`, и в Adapty
    Dashboard. Сравнение constant-time.

    Незаданный секрет → 500, а НЕ «пропустить»: пустая строка не должна совпадать ни с чем,
    а Adapty обязан ретраить, пока оператор не задаст секрет. Неверный/отсутствующий токен →
    401 без раскрытия причины.
    """
    settings: Settings = request.app.state.settings
    secret = settings.ADAPTY_WEBHOOK_SECRET.get_secret_value()
    if not secret:
        raise APIError(
            "Adapty webhook secret not configured",
            code="ADAPTY_WEBHOOK_MISCONFIGURED",
            http_status=500,
        )
    presented = credentials.credentials.strip() if credentials else None
    if not presented or not hmac.compare_digest(presented, secret):
        raise AuthError(message="Invalid adapty webhook token")


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
