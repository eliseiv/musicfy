from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.deps import get_analytics_service, get_current_user
from app.domain.models.user import User
from app.domain.services.analytics_service import AnalyticsService
from app.schemas.common import CamelModel


class TrackEventRequest(CamelModel):
    name: str
    properties: dict | None = None


router = APIRouter(tags=["Аналитика"])


@router.post("/analytics/events", status_code=204, summary="Записать событие воронки")
async def track_event(
    body: TrackEventRequest,
    current: Annotated[User, Depends(get_current_user)],
    analytics: Annotated[AnalyticsService, Depends(get_analytics_service)],
) -> None:
    await analytics.track(user_id=current.id, name=body.name, properties=body.properties)


class LegalNotice(CamelModel):
    key: str
    title: str
    body: str


@router.get("/legal/notices", response_model=list[LegalNotice], summary="Правовые уведомления")
async def legal_notices() -> list[LegalNotice]:
    """Copyright / voice-rights уведомления для UI."""
    return [
        LegalNotice(
            key="voice_rights",
            title="Voice rights",
            body=(
                "You may only clone a voice you own or have explicit permission to use. "
                "Cloning a real person's voice without consent is prohibited."
            ),
        ),
        LegalNotice(
            key="copyright",
            title="Copyright",
            body=(
                "Do not upload copyrighted audio you do not have the rights to. "
                "Generated covers are for personal use; you are responsible for how you use them."
            ),
        ),
    ]
