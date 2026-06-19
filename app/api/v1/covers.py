from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header

from app.deps import get_current_user, get_generation_service
from app.domain.enums import JobType
from app.domain.models.user import User
from app.domain.schemas.covers import CreateCoverRequest
from app.domain.schemas.songs import JobAcceptedResponse
from app.domain.services.generation_service import GenerationService

router = APIRouter(prefix="/covers", tags=["Кавер"])


@router.post("", response_model=JobAcceptedResponse, status_code=202, summary="Создать AI cover")
async def create_cover(
    body: CreateCoverRequest,
    current: Annotated[User, Depends(get_current_user)],
    generation: Annotated[GenerationService, Depends(get_generation_service)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)] = None,
) -> JobAcceptedResponse:
    payload = body.model_dump(exclude_none=True, by_alias=False)
    store_stems = bool(payload.pop("store_stems", False))
    result = await generation.create_job(
        user_id=current.id,
        job_type=JobType.cover,
        payload=payload,
        store_stems=store_stems,
        client_idempotency_key=idempotency_key,
    )
    return JobAcceptedResponse(
        job_id=str(result.job_id), status="queued", deduplicated=result.deduplicated
    )
