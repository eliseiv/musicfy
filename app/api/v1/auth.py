from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Security
from fastapi.security import HTTPAuthorizationCredentials

from app.auth.sessions import AuthService, SessionResult
from app.deps import (
    bearer_scheme,
    get_auth_service,
    get_current_user,
    get_optional_user,
)
from app.domain.models.user import User
from app.domain.schemas.auth import (
    AppleSignInRequest,
    GuestSignInRequest,
    MeResponse,
    SessionResponse,
)

router = APIRouter(prefix="/auth", tags=["Авторизация"])


def _to_response(result: SessionResult) -> SessionResponse:
    return SessionResponse(
        user_id=str(result.user_id),
        is_guest=result.is_guest,
        display_name=result.display_name,
        token=result.token,
        expires_at=result.expires_at,
    )


@router.post("/guest", response_model=SessionResponse, summary="Создать guest-сессию")
async def guest_sign_in(
    body: GuestSignInRequest,
    auth: Annotated[AuthService, Depends(get_auth_service)],
) -> SessionResponse:
    result = await auth.create_guest(device_id=body.device_id)
    return _to_response(result)


@router.post("/apple", response_model=SessionResponse, summary="Sign in with Apple")
async def apple_sign_in(
    body: AppleSignInRequest,
    auth: Annotated[AuthService, Depends(get_auth_service)],
    current: Annotated[User | None, Depends(get_optional_user)],
) -> SessionResponse:
    result = await auth.sign_in_with_apple(
        identity_token=body.identity_token,
        nonce=body.nonce,
        display_name=body.display_name,
        current_user_id=current.id if current else None,
    )
    return _to_response(result)


@router.post("/logout", status_code=204, summary="Выйти (отозвать сессию)")
async def logout(
    auth: Annotated[AuthService, Depends(get_auth_service)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer_scheme)] = None,
) -> None:
    if credentials and credentials.credentials:
        await auth.logout(credentials.credentials.strip())


@router.get("/me", response_model=MeResponse, summary="Текущий пользователь", tags=["Авторизация"])
async def me(current: Annotated[User, Depends(get_current_user)]) -> MeResponse:
    return MeResponse(
        user_id=str(current.id),
        is_guest=current.is_guest,
        display_name=current.display_name,
    )
