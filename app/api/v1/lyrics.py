from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header

from app.deps import get_current_user, get_lyrics_service
from app.domain.models.user import User
from app.domain.schemas.lyrics import (
    GenerateLyricsRequest,
    LyricsDraftResponse,
    UpdateLyricsRequest,
)
from app.domain.services.lyrics_service import LyricsService

router = APIRouter(prefix="/lyrics", tags=["Текст песни"])


def _to_response(draft) -> LyricsDraftResponse:
    return LyricsDraftResponse(
        id=str(draft.id),
        content=draft.content,
        language=draft.language,
        genre=draft.genre,
        mood=draft.mood,
        source=draft.source,
        created_at=draft.created_at,
    )


@router.post("", response_model=LyricsDraftResponse, summary="Сгенерировать текст")
async def generate_lyrics(
    body: GenerateLyricsRequest,
    current: Annotated[User, Depends(get_current_user)],
    lyrics: Annotated[LyricsService, Depends(get_lyrics_service)],
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", max_length=128)
    ] = None,
) -> LyricsDraftResponse:
    draft = await lyrics.generate(
        user_id=current.id,
        prompt=body.prompt,
        language=body.language,
        genre=body.genre,
        mood=body.mood,
        idempotency_key=idempotency_key,
    )
    return _to_response(draft)


@router.get("/{draft_id}", response_model=LyricsDraftResponse, summary="Получить текст")
async def get_lyrics(
    draft_id: UUID,
    current: Annotated[User, Depends(get_current_user)],
    lyrics: Annotated[LyricsService, Depends(get_lyrics_service)],
) -> LyricsDraftResponse:
    draft = await lyrics.get(user_id=current.id, draft_id=draft_id)
    return _to_response(draft)


@router.patch("/{draft_id}", response_model=LyricsDraftResponse, summary="Отредактировать текст")
async def update_lyrics(
    draft_id: UUID,
    body: UpdateLyricsRequest,
    current: Annotated[User, Depends(get_current_user)],
    lyrics: Annotated[LyricsService, Depends(get_lyrics_service)],
) -> LyricsDraftResponse:
    draft = await lyrics.update_content(
        user_id=current.id, draft_id=draft_id, content=body.content
    )
    return _to_response(draft)
