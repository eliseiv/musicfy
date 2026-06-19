from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import WebhookProvider
from app.domain.models.webhook import ProcessedWebhook


class WebhooksRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def try_record(
        self,
        *,
        provider: WebhookProvider,
        event_id: str,
        payload_digest: str,
    ) -> bool:
        """Claim события. True — заклеймили (новое), False — дубликат."""
        stmt = (
            pg_insert(ProcessedWebhook)
            .values(
                provider=provider,
                event_id=event_id,
                payload_digest=payload_digest,
                outcome="received",
            )
            .on_conflict_do_nothing(index_elements=["provider", "event_id"])
            .returning(ProcessedWebhook.event_id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def mark_applied(
        self, *, provider: WebhookProvider, event_id: str
    ) -> None:
        await self._session.execute(
            update(ProcessedWebhook)
            .where(
                ProcessedWebhook.provider == provider,
                ProcessedWebhook.event_id == event_id,
            )
            .values(outcome="applied", applied_at=datetime.now(UTC))
        )

    async def list_received(self, *, limit: int = 500) -> list[ProcessedWebhook]:
        stmt = (
            select(ProcessedWebhook)
            .where(ProcessedWebhook.outcome == "received")
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars().all())
