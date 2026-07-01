from __future__ import annotations

import logging
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.domain.enums import JobStage, JobStatus
from app.domain.models.job import Job
from app.domain.providers.fal.base import FalProvider
from app.domain.repositories.jobs import JobsRepository

logger = logging.getLogger(__name__)


class CreditGate(Protocol):
    """Интерфейс списания монет. Реализуется CoinWalletService."""

    async def capture(self, *, job: Job) -> int: ...

    async def release(self, *, job: Job) -> None: ...


class BasePipeline:
    """Общая инфраструктура для всех job-пайплайнов.

    Подклассы реализуют `start`, `advance`, `fail` и `webhook_completed_status`.
    Здесь — общие операции над job/stage_log/runtime и работа с кредитами.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        fal: FalProvider,
        settings: Settings,
        *,
        credits: CreditGate | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._fal = fal
        self._settings = settings
        self._credits = credits

    # ---- абстрактные ----

    async def start(self, job: Job) -> None:  # pragma: no cover - интерфейс
        raise NotImplementedError

    async def advance(
        self,
        *,
        job: Job,
        completed_stage: JobStage,
        media_url: str | None,
        duration_seconds: float | None,
        stems: dict[str, Any] | None,
        event_id: str,
    ) -> None:  # pragma: no cover
        raise NotImplementedError

    async def fail(
        self, *, job: Job, failed_stage: JobStage, error_code: str, error_message: str
    ) -> None:  # pragma: no cover
        raise NotImplementedError

    # ---- общие helpers ----

    async def load_job(self, job_id: UUID) -> Job | None:
        async with self._sessionmaker() as session:
            job = await session.get(Job, job_id)
            if job is not None:
                session.expunge(job)
            return job

    async def _record_stage(
        self, job_id: UUID, stage: JobStage, status: str, *, error: str | None = None
    ) -> None:
        async with self._sessionmaker() as session:
            async with session.begin():
                await JobsRepository(session).record_stage_event(
                    job_id=job_id, stage=stage, status=status, error=error
                )

    async def _list_recorded_stages(self, job_id: UUID) -> set[JobStage]:
        async with self._sessionmaker() as session:
            events = await JobsRepository(session).list_stage_events(job_id)
            return {e.stage for e in events}

    async def _set_current_stage(
        self, job_id: UUID, stage: JobStage, request_id: str,
        *, provider_model: str | None = None, submit=None,
    ) -> None:
        async with self._sessionmaker() as session:
            async with session.begin():
                repo = JobsRepository(session)
                await repo.set_current_stage(
                    job_id=job_id,
                    stage=stage,
                    provider_request_id=request_id,
                    provider_model=provider_model,
                )
                # Сохраняем реальные status_url/response_url из ответа submit —
                # poller опрашивает их напрямую (надёжнее versioned-пути).
                if submit is not None and (submit.status_url or submit.response_url):
                    job = await repo.get_by_id_for_update(job_id)
                    if job is not None:
                        payload = dict(job.input_payload or {})
                        payload["_fal_status_url"] = submit.status_url
                        payload["_fal_response_url"] = submit.response_url
                        job.input_payload = payload

    async def _update_payload(self, job_id: UUID, patch: dict[str, Any]) -> None:
        async with self._sessionmaker() as session:
            async with session.begin():
                repo = JobsRepository(session)
                job = await repo.get_by_id_for_update(job_id)
                if job is None:
                    return
                payload = dict(job.input_payload or {})
                payload.update(patch)
                job.input_payload = payload

    async def _persist_runtime(self, job_id: UUID, runtime: dict[str, Any]) -> None:
        await self._update_payload(job_id, {"_runtime": runtime})

    async def _get_runtime(self, job_id: UUID) -> dict[str, Any]:
        async with self._sessionmaker() as session:
            job = await session.get(Job, job_id)
            return dict((job.input_payload or {}).get("_runtime") or {}) if job else {}

    async def _mark_status(self, job_id: UUID, status: JobStatus) -> None:
        async with self._sessionmaker() as session:
            async with session.begin():
                await JobsRepository(session).set_status(job_id=job_id, status=status)

    async def _mark_failed(
        self, job_id: UUID, error_code: str, error_message: str
    ) -> None:
        # Возврат зарезервированных кредитов (если кредитный шлюз подключён).
        if self._credits is not None:
            job = await self.load_job(job_id)
            if job is not None:
                try:
                    await self._credits.release(job=job)
                except Exception:
                    logger.exception("credit release failed for job=%s", job_id)
        async with self._sessionmaker() as session:
            async with session.begin():
                await JobsRepository(session).mark_failed(
                    job_id=job_id, error_code=error_code, error_message=error_message
                )

    async def _capture_credits(self, job_id: UUID) -> int:
        if self._credits is None:
            return 0
        job = await self.load_job(job_id)
        if job is None:
            return 0
        try:
            return await self._credits.capture(job=job)
        except Exception:
            logger.exception("credit capture failed for job=%s", job_id)
            return 0

    def _webhook_url(self) -> str | None:
        base = (self._settings.PUBLIC_BASE_URL or "").rstrip("/")
        return f"{base}/v1/webhooks/fal" if base else None
