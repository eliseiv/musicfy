from __future__ import annotations

import uuid

from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy import (
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import ModerationStatus
from app.models.base import Base, TimestampMixin


class ModerationCase(Base, TimestampMixin):
    """Кейс модерации контента."""

    __tablename__ = "moderation_cases"
    __table_args__ = (
        Index("ix_moderation_cases_user_id_created_at", "user_id", "created_at"),
        Index("ix_moderation_cases_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True,
        server_default=text("gen_random_uuid()"), default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    status: Mapped[ModerationStatus] = mapped_column(
        SAEnum(ModerationStatus, name="moderation_status", native_enum=True), nullable=False
    )
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
