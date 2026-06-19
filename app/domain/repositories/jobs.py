from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    ACTIVE_JOB_STATUSES,
    JobStage,
    JobStatus,
    JobType,
    StageStatus,
)
from app.domain.models.job import Job, JobStageLog


class JobsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        user_id: UUID,
        job_type: JobType,
        input_payload: dict[str, Any],
        provider_model: str | None,
        credit_category: Any | None = None,
        reserved_credits: int = 0,
        store_stems: bool = False,
        client_idempotency_key: str | None = None,
    ) -> Job:
        job = Job(
            user_id=user_id,
            job_type=job_type,
            status=JobStatus.created,
            input_payload=input_payload,
            provider_model=provider_model,
            credit_category=credit_category,
            reserved_credits=reserved_credits,
            store_stems=store_stems,
            client_idempotency_key=client_idempotency_key,
        )
        self._session.add(job)
        await self._session.flush()
        return job

    async def get_by_id(self, job_id: UUID) -> Job | None:
        return await self._session.get(Job, job_id)

    async def get_by_id_for_update(self, job_id: UUID) -> Job | None:
        stmt = select(Job).where(Job.id == job_id).with_for_update()
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_idempotency_key(
        self, *, user_id: UUID, key: str
    ) -> Job | None:
        stmt = select(Job).where(
            Job.user_id == user_id, Job.client_idempotency_key == key
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def find_by_request_id(self, request_id: str) -> Job | None:
        stmt = select(Job).where(Job.provider_request_id == request_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_user(
        self, *, user_id: UUID, limit: int = 50, offset: int = 0,
        job_type: JobType | None = None,
    ) -> list[Job]:
        stmt = select(Job).where(Job.user_id == user_id)
        if job_type is not None:
            stmt = stmt.where(Job.job_type == job_type)
        stmt = stmt.order_by(Job.created_at.desc()).limit(limit).offset(offset)
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_active_with_request(self, limit: int = 50) -> list[Job]:
        stmt = (
            select(Job)
            .where(
                Job.status.in_(list(ACTIVE_JOB_STATUSES)),
                Job.provider_request_id.is_not(None),
            )
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_orphans(self) -> list[Job]:
        """queued/running без provider_request_id — застряли при submit."""
        stmt = select(Job).where(
            Job.status.in_([JobStatus.queued, JobStatus.running]),
            Job.provider_request_id.is_(None),
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def set_status(self, *, job_id: UUID, status: JobStatus) -> None:
        await self._session.execute(
            update(Job).where(Job.id == job_id).values(status=status)
        )

    async def set_current_stage(
        self, *, job_id: UUID, stage: JobStage, provider_request_id: str,
        provider_model: str | None = None,
    ) -> None:
        values: dict[str, Any] = {
            "current_stage": stage,
            "stage": stage,
            "provider_request_id": provider_request_id,
            "status": JobStatus.running,
        }
        if provider_model is not None:
            values["provider_model"] = provider_model
        await self._session.execute(update(Job).where(Job.id == job_id).values(**values))

    async def mark_succeeded(self, *, job_id: UUID, captured_credits: int) -> None:
        await self._session.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(
                status=JobStatus.completed,
                captured_credits=captured_credits,
                finished_at=datetime.now(UTC),
            )
        )

    async def mark_failed(
        self, *, job_id: UUID, error_code: str, error_message: str
    ) -> None:
        await self._session.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(
                status=JobStatus.failed,
                error_code=error_code,
                error_message=error_message[:2000] if error_message else None,
                finished_at=datetime.now(UTC),
            )
        )

    # --- stage log ---

    async def record_stage_event(
        self, *, job_id: UUID, stage: JobStage, status: str, error: str | None = None
    ) -> None:
        stmt = pg_insert(JobStageLog).values(
            job_id=job_id, stage=stage, status=status, error=error
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["job_id", "stage"],
            set_={"status": status, "error": error},
        )
        await self._session.execute(stmt)

    async def list_stage_events(self, job_id: UUID) -> list[JobStageLog]:
        stmt = select(JobStageLog).where(JobStageLog.job_id == job_id)
        return list((await self._session.execute(stmt)).scalars().all())


def stage_status(value: str) -> StageStatus:
    return StageStatus(value)
