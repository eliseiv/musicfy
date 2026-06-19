from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.deps import get_sessionmaker
from app.domain.enums import PresetKind
from app.domain.repositories.presets import PresetsRepository
from app.domain.schemas.tracks import PresetView

router = APIRouter(prefix="/presets", tags=["Пресеты"])


async def _list(sessionmaker, kind: PresetKind) -> list[PresetView]:
    async with sessionmaker() as session:
        rows = await PresetsRepository(session).list_by_kind(kind)
        return [
            PresetView(
                key=r.key, title=r.title, subtitle=r.subtitle, prompt_text=r.prompt_text
            )
            for r in rows
        ]


@router.get("/genres", response_model=list[PresetView], summary="Жанры")
async def genres(sessionmaker: Annotated[object, Depends(get_sessionmaker)]):
    return await _list(sessionmaker, PresetKind.genre)


@router.get("/moods", response_model=list[PresetView], summary="Настроения")
async def moods(sessionmaker: Annotated[object, Depends(get_sessionmaker)]):
    return await _list(sessionmaker, PresetKind.mood)


@router.get("/prompts", response_model=list[PresetView], summary="Промпт-пресеты")
async def prompts(sessionmaker: Annotated[object, Depends(get_sessionmaker)]):
    return await _list(sessionmaker, PresetKind.prompt)
