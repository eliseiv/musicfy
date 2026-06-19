from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.repositories.jobs import JobsRepository
from app.domain.repositories.webhooks import WebhooksRepository

logger = logging.getLogger(__name__)


async def recover_orphan_jobs(
    *, sessionmaker: async_sessionmaker[AsyncSession], credits=None
) -> int:
    """Помечает queued/running без provider_request_id как failed и возвращает кредиты."""
    async with sessionmaker() as session:
        orphans = await JobsRepository(session).list_orphans()
        for o in orphans:
            session.expunge(o)
    if not orphans:
        return 0
    for job in orphans:
        try:
            if credits is not None and job.reserved_credits:
                await credits.release(job=job)
            async with sessionmaker() as session:
                async with session.begin():
                    await JobsRepository(session).mark_failed(
                        job_id=job.id,
                        error_code="STARTUP_RECOVERY",
                        error_message="job was queued without provider_request_id",
                    )
            logger.info("Recovered orphan job %s", job.id)
        except Exception:
            logger.exception("Failed to recover orphan job %s", job.id)
    return len(orphans)


async def report_received_webhooks(
    *, sessionmaker: async_sessionmaker[AsyncSession]
) -> int:
    async with sessionmaker() as session:
        stuck = await WebhooksRepository(session).list_received(limit=500)
    for w in stuck:
        logger.warning(
            "Webhook stuck in 'received': provider=%s event_id=%s received_at=%s",
            getattr(w.provider, "value", w.provider), w.event_id, w.received_at,
        )
    return len(stuck)
