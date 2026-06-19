from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import CreditCategory, JobStage, JobStatus, JobType
from app.models.base import Base, TimestampMixin


class Job(Base, TimestampMixin):
    """Единая задача генерации (song / lyrics / cover / voice_clone / video)."""

    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_user_id_created_at", "user_id", "created_at"),
        Index(
            "ix_jobs_active_status",
            "status",
            postgresql_where=text(
                "status IN ('created','queued','running','post_processing')"
            ),
        ),
        Index(
            "ix_jobs_provider_request_id",
            "provider_request_id",
            postgresql_where=text("provider_request_id IS NOT NULL"),
        ),
        Index(
            "uq_jobs_user_id_client_idempotency_key",
            "user_id",
            "client_idempotency_key",
            unique=True,
            postgresql_where=text("client_idempotency_key IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    job_type: Mapped[JobType] = mapped_column(
        SAEnum(JobType, name="job_type", native_enum=True), nullable=False
    )
    status: Mapped[JobStatus] = mapped_column(
        SAEnum(JobStatus, name="job_status", native_enum=True), nullable=False
    )
    stage: Mapped[JobStage | None] = mapped_column(
        SAEnum(JobStage, name="job_stage", native_enum=True), nullable=True
    )
    # current_stage — последняя запущенная async-стадия (для идемпотентного webhook).
    current_stage: Mapped[JobStage | None] = mapped_column(
        SAEnum(JobStage, name="job_stage", native_enum=True, create_type=False),
        nullable=True,
    )
    provider_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(160), nullable=True)

    # Категория кредитов (None для lyrics / voice_clone — не списываются).
    credit_category: Mapped[CreditCategory | None] = mapped_column(
        SAEnum(CreditCategory, name="credit_category", native_enum=True), nullable=True
    )
    reserved_credits: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    captured_credits: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )

    input_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    store_stems: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    client_idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class JobStageLog(Base, TimestampMixin):
    """Append-/upsert-лог стадий пайплайна. Одна строка на (job_id, stage)."""

    __tablename__ = "job_stage_log"
    __table_args__ = (
        Index("uq_job_stage_log_job_id_stage", "job_id", "stage", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        default=uuid.uuid4,
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    stage: Mapped[JobStage] = mapped_column(
        SAEnum(JobStage, name="job_stage", native_enum=True, create_type=False),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
