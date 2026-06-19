from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.deps import get_current_user, get_sessionmaker
from app.domain.models.device import DevicePushToken
from app.domain.models.user import User
from app.domain.schemas.videos import PushTokenRequest

router = APIRouter(prefix="/devices", tags=["Устройства"])


@router.post("/push-token", status_code=204, summary="Зарегистрировать APNs-токен")
async def register_push_token(
    body: PushTokenRequest,
    current: Annotated[User, Depends(get_current_user)],
    sessionmaker: Annotated[object, Depends(get_sessionmaker)],
) -> None:
    async with sessionmaker() as session:
        async with session.begin():
            stmt = (
                pg_insert(DevicePushToken)
                .values(user_id=current.id, token=body.token, platform=body.platform)
                .on_conflict_do_update(
                    constraint="uq_device_push_tokens_token",
                    set_={"user_id": current.id, "platform": body.platform},
                )
            )
            await session.execute(stmt)
