from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.models.usage_event import UsageEvent

logger = logging.getLogger(__name__)

# Допустимые имена событий воронки.
KNOWN_EVENTS = {
    "onboarding_completed",
    "generation_started",
    "generation_succeeded",
    "generation_failed",
    "paywall_viewed",
    "purchase_subscription",
    "purchase_pack",
    "cover_started",
    "clone_voice_started",
    "video_started",
}


class AnalyticsService:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def track(
        self, *, user_id: UUID | None, name: str, properties: dict[str, Any] | None = None
    ) -> None:
        try:
            async with self._sessionmaker() as session:
                async with session.begin():
                    session.add(
                        UsageEvent(user_id=user_id, name=name[:64], properties=properties)
                    )
        except Exception:
            logger.exception("analytics track failed: %s", name)

    async def list_events(
        self, *, user_id: UUID, limit: int = 100
    ) -> list[UsageEvent]:
        async with self._sessionmaker() as session:
            stmt = (
                select(UsageEvent)
                .where(UsageEvent.user_id == user_id)
                .order_by(UsageEvent.created_at.desc())
                .limit(limit)
            )
            return list((await session.execute(stmt)).scalars().all())
