from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import TrackKind
from app.models.base import Base, TimestampMixin


class Track(Base, TimestampMixin):
    """Результат генерации song/cover. Содержит 1..N вариантов (TrackVariant)."""

    __tablename__ = "tracks"
    __table_args__ = (
        Index("ix_tracks_user_id_created_at", "user_id", "created_at"),
        Index("ix_tracks_job_id", "job_id"),
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
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="SET NULL"),
        nullable=True,
    )
    kind: Mapped[TrackKind] = mapped_column(
        SAEnum(TrackKind, name="track_kind", native_enum=True), nullable=False
    )
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class TrackVariant(Base, TimestampMixin):
    __tablename__ = "track_variants"
    __table_args__ = (
        Index("ix_track_variants_track_id", "track_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        default=uuid.uuid4,
    )
    track_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tracks.id", ondelete="CASCADE"),
        nullable=False,
    )
    variant_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    audio_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    duration_seconds: Mapped[Decimal] = mapped_column(
        Numeric(10, 3), nullable=False, default=0
    )
    stems: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
