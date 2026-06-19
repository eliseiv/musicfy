"""Polling worker — фоновая задача, опрашивающая fal queue API.

Fallback к webhook'ам: каждые POLL_INTERVAL_SECONDS берёт активные jobs с
provider_request_id и опрашивает их статус. Идемпотентно (advance проверяет
current_stage).
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.errors import FalProviderError, FalTimeout
from app.config import Settings
from app.domain.enums import JobStage
from app.domain.providers.fal.base import FalProvider, FalStatusResult
from app.domain.repositories.jobs import JobsRepository
from app.domain.services.pipelines.runner import PipelineRunner

logger = logging.getLogger(__name__)


class FalPoller:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        fal: FalProvider,
        runner: PipelineRunner,
        settings: Settings,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._fal = fal
        self._runner = runner
        self._settings = settings
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="fal-poller")
            logger.info(
                "FalPoller started (interval=%ss)", self._settings.POLL_INTERVAL_SECONDS
            )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        interval = max(2.0, float(self._settings.POLL_INTERVAL_SECONDS))
        while not self._stop.is_set():
            try:
                await self._poll_once()
            except Exception:
                logger.exception("FalPoller iteration failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                pass

    async def _poll_once(self) -> None:
        async with self._sessionmaker() as session:
            jobs = await JobsRepository(session).list_active_with_request(limit=50)
            snapshots = [
                {
                    "id": j.id,
                    "provider_model": j.provider_model,
                    "provider_request_id": j.provider_request_id,
                    "current_stage": j.current_stage,
                    "status_url": (j.input_payload or {}).get("_fal_status_url"),
                    "response_url": (j.input_payload or {}).get("_fal_response_url"),
                }
                for j in jobs
            ]
        for j in snapshots:
            try:
                result = await self._fal.fetch_status(
                    model=j["provider_model"],
                    request_id=j["provider_request_id"],
                    status_url=j["status_url"],
                    response_url=j["response_url"],
                )
            except (FalProviderError, FalTimeout) as exc:
                logger.warning(
                    "FalPoller fetch_status failed job=%s: %s", j["id"], exc
                )
                continue
            await self._apply(j, result)

    async def _apply(self, j: dict, result: FalStatusResult) -> None:
        status = result.status.upper()
        if status in ("IN_QUEUE", "IN_PROGRESS"):
            return
        current_stage: JobStage = j["current_stage"] or JobStage.music_generation
        if status == "COMPLETED":
            await self._runner.advance(
                job_id=j["id"],
                completed_stage=current_stage,
                media_url=result.media_url,
                duration_seconds=result.duration_seconds,
                stems=result.stems,
                event_id=f"poll:{j['provider_request_id']}",
            )
        elif status in ("FAILED", "CANCELED", "ERROR"):
            await self._runner.fail(
                job_id=j["id"],
                failed_stage=current_stage,
                error_code="PROVIDER_FAILED" if status != "CANCELED" else "PROVIDER_CANCELED",
                error_message=result.error_message or status,
            )
