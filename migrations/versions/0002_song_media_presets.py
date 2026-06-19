"""song/media/presets: jobs, job_stage_log, tracks, track_variants, lyrics_drafts,
prompt_presets, assets

Revision ID: 0002_song_media_presets
Revises: 0001_core_identity
Create Date: 2026-06-18

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_song_media_presets"
down_revision: str | None = "0001_core_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


JOB_TYPE = ("song", "lyrics", "cover", "voice_clone", "video")
JOB_STATUS = ("created", "queued", "running", "post_processing", "completed", "failed", "canceled")
JOB_STAGE = (
    "prepare_prompt", "upload_cdn", "finalize", "lyrics", "music_generation",
    "vocal_tts", "mix_master", "stem_separation", "voice_conversion",
    "consent_check", "quality_check", "voice_clone", "source_prep", "lipsync",
)
CREDIT_CATEGORY = ("song", "cover", "video")
TRACK_KIND = ("song", "cover")
ASSET_KIND = ("audio", "video", "voice_sample", "source_video", "stem", "image")
PRESET_KIND = ("genre", "mood", "prompt")


def _ts(*extra):
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        *extra,
    )


def upgrade() -> None:
    bind = op.get_bind()
    job_type = postgresql.ENUM(*JOB_TYPE, name="job_type", create_type=False)
    job_status = postgresql.ENUM(*JOB_STATUS, name="job_status", create_type=False)
    job_stage = postgresql.ENUM(*JOB_STAGE, name="job_stage", create_type=False)
    credit_category = postgresql.ENUM(*CREDIT_CATEGORY, name="credit_category", create_type=False)
    track_kind = postgresql.ENUM(*TRACK_KIND, name="track_kind", create_type=False)
    asset_kind = postgresql.ENUM(*ASSET_KIND, name="asset_kind", create_type=False)
    preset_kind = postgresql.ENUM(*PRESET_KIND, name="preset_kind", create_type=False)
    for e in (job_type, job_status, job_stage, credit_category, track_kind, asset_kind, preset_kind):
        e.create(bind, checkfirst=True)

    op.create_table(
        "jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_type", job_type, nullable=False),
        sa.Column("status", job_status, nullable=False),
        sa.Column("stage", job_stage, nullable=True),
        sa.Column("current_stage", job_stage, nullable=True),
        sa.Column("provider_model", sa.String(length=128), nullable=True),
        sa.Column("provider_request_id", sa.String(length=160), nullable=True),
        sa.Column("credit_category", credit_category, nullable=True),
        sa.Column("reserved_credits", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("captured_credits", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("input_payload", postgresql.JSONB(), nullable=False),
        sa.Column("store_stems", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("client_idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_jobs_user_id_users", ondelete="CASCADE"),
    )
    op.create_index("ix_jobs_user_id_created_at", "jobs", ["user_id", "created_at"])
    op.create_index(
        "ix_jobs_active_status", "jobs", ["status"],
        postgresql_where=sa.text("status IN ('created','queued','running','post_processing')"),
    )
    op.create_index(
        "ix_jobs_provider_request_id", "jobs", ["provider_request_id"],
        postgresql_where=sa.text("provider_request_id IS NOT NULL"),
    )
    op.create_index(
        "uq_jobs_user_id_client_idempotency_key", "jobs",
        ["user_id", "client_idempotency_key"], unique=True,
        postgresql_where=sa.text("client_idempotency_key IS NOT NULL"),
    )

    op.create_table(
        "job_stage_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stage", job_stage, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        *_ts(),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], name="fk_job_stage_log_job_id_jobs", ondelete="CASCADE"),
    )
    op.create_index("uq_job_stage_log_job_id_stage", "job_stage_log", ["job_id", "stage"], unique=True)

    op.create_table(
        "tracks",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", track_kind, nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("meta", postgresql.JSONB(), nullable=True),
        *_ts(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_tracks_user_id_users", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], name="fk_tracks_job_id_jobs", ondelete="SET NULL"),
    )
    op.create_index("ix_tracks_user_id_created_at", "tracks", ["user_id", "created_at"])
    op.create_index("ix_tracks_job_id", "tracks", ["job_id"])

    op.create_table(
        "track_variants",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("track_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("variant_index", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("audio_url", sa.String(length=1024), nullable=False),
        sa.Column("duration_seconds", sa.Numeric(10, 3), nullable=False),
        sa.Column("stems", postgresql.JSONB(), nullable=True),
        sa.Column("meta", postgresql.JSONB(), nullable=True),
        *_ts(),
        sa.ForeignKeyConstraint(["track_id"], ["tracks.id"], name="fk_track_variants_track_id_tracks", ondelete="CASCADE"),
    )
    op.create_index("ix_track_variants_track_id", "track_variants", ["track_id"])

    op.create_table(
        "lyrics_drafts",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("language", sa.String(length=8), server_default=sa.text("'en'"), nullable=False),
        sa.Column("genre", sa.String(length=64), nullable=True),
        sa.Column("mood", sa.String(length=64), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=16), server_default=sa.text("'generated'"), nullable=False),
        sa.Column("meta", postgresql.JSONB(), nullable=True),
        *_ts(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_lyrics_drafts_user_id_users", ondelete="CASCADE"),
    )
    op.create_index("ix_lyrics_drafts_user_id_created_at", "lyrics_drafts", ["user_id", "created_at"])

    op.create_table(
        "prompt_presets",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("kind", preset_kind, nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=128), nullable=False),
        sa.Column("subtitle", sa.String(length=255), nullable=True),
        sa.Column("prompt_text", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("meta", postgresql.JSONB(), nullable=True),
        *_ts(),
    )
    op.create_index("uq_prompt_presets_kind_key", "prompt_presets", ["kind", "key"], unique=True)
    op.create_index("ix_prompt_presets_kind_active_sort", "prompt_presets", ["kind", "active", "sort_order"])

    op.create_table(
        "assets",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", asset_kind, nullable=False),
        sa.Column("url", sa.String(length=1024), nullable=False),
        sa.Column("mime", sa.String(length=128), nullable=True),
        sa.Column("duration_seconds", sa.Numeric(10, 3), nullable=True),
        sa.Column("size_bytes", sa.Numeric(20, 0), nullable=True),
        sa.Column("meta", postgresql.JSONB(), nullable=True),
        *_ts(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_assets_user_id_users", ondelete="CASCADE"),
    )
    op.create_index("ix_assets_user_id_created_at", "assets", ["user_id", "created_at"])
    op.create_index("ix_assets_kind", "assets", ["kind"])


def downgrade() -> None:
    op.drop_table("assets")
    op.drop_table("prompt_presets")
    op.drop_table("lyrics_drafts")
    op.drop_table("track_variants")
    op.drop_table("tracks")
    op.drop_table("job_stage_log")
    op.drop_table("jobs")
    for name in ("preset_kind", "asset_kind", "track_kind", "credit_category", "job_stage", "job_status", "job_type"):
        postgresql.ENUM(name=name).drop(op.get_bind(), checkfirst=True)
