from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.enums import ModerationStatus
from app.domain.models.moderation import ModerationCase

logger = logging.getLogger(__name__)

# Базовый блок-лист V1. В проде — заменить на внешний moderation-провайдер.
_BLOCKLIST = {
    "child sexual",
    "csam",
    "bomb instructions",
    "terrorist attack",
}


class ModerationService:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    @staticmethod
    def screen_text(*texts: str | None) -> str | None:
        """Возвращает причину блокировки или None если контент допустим."""
        joined = " ".join(t.lower() for t in texts if t)
        for phrase in _BLOCKLIST:
            if phrase in joined:
                return f"blocked_phrase:{phrase}"
        return None

    async def record_case(
        self,
        *,
        user_id: UUID | None,
        status: ModerationStatus,
        reason: str | None,
        excerpt: str | None,
    ) -> None:
        async with self._sessionmaker() as session:
            async with session.begin():
                session.add(
                    ModerationCase(
                        user_id=user_id,
                        status=status,
                        reason=reason,
                        content_excerpt=(excerpt or "")[:500] or None,
                    )
                )
