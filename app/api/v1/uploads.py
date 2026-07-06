from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, UploadFile

from app.config import Settings
from app.deps import get_asset_service, get_current_user, get_settings_dep
from app.domain.enums import AssetKind
from app.domain.models.user import User
from app.domain.schemas.covers import AssetResponse
from app.domain.services.asset_service import AssetService

router = APIRouter(prefix="/uploads", tags=["Загрузка"])


async def _store(
    *, file: UploadFile, kind: AssetKind, allowed: set[str],
    assets: AssetService, settings: Settings, user: User,
) -> AssetResponse:
    # Ранний отказ по размеру (до чтения в память), если клиент сообщил размер.
    if file.size is not None and file.size > settings.UPLOAD_MAX_BYTES:
        from app.api.errors import UploadRejected

        raise UploadRejected(
            details={"reason": "too_large", "max_bytes": settings.UPLOAD_MAX_BYTES}
        )
    content = await file.read()
    asset = await assets.upload(
        user_id=user.id,
        content=content,
        filename=file.filename or "upload",
        content_type=file.content_type or "application/octet-stream",
        kind=kind,
        max_bytes=settings.UPLOAD_MAX_BYTES,
        allowed_content_types=allowed,
    )
    return AssetResponse(
        asset_id=str(asset.id), url=asset.url, kind=asset.kind.value, mime=asset.mime
    )


@router.post("/audio", response_model=AssetResponse, summary="Загрузить аудио (для cover)")
async def upload_audio(
    current: Annotated[User, Depends(get_current_user)],
    assets: Annotated[AssetService, Depends(get_asset_service)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    file: Annotated[UploadFile, File()],
) -> AssetResponse:
    return await _store(
        file=file, kind=AssetKind.audio, allowed=settings.upload_audio_content_types,
        assets=assets, settings=settings, user=current,
    )


@router.post("/voice", response_model=AssetResponse, summary="Загрузить образец голоса")
async def upload_voice(
    current: Annotated[User, Depends(get_current_user)],
    assets: Annotated[AssetService, Depends(get_asset_service)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    file: Annotated[UploadFile, File()],
) -> AssetResponse:
    return await _store(
        file=file, kind=AssetKind.voice_sample, allowed=settings.upload_audio_content_types,
        assets=assets, settings=settings, user=current,
    )


@router.post("/source-video", response_model=AssetResponse, summary="Загрузить видео (для video)")
async def upload_source_video(
    current: Annotated[User, Depends(get_current_user)],
    assets: Annotated[AssetService, Depends(get_asset_service)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    file: Annotated[UploadFile, File()],
) -> AssetResponse:
    return await _store(
        file=file, kind=AssetKind.source_video, allowed=settings.upload_video_content_types,
        assets=assets, settings=settings, user=current,
    )


@router.post("/image", response_model=AssetResponse, summary="Загрузить картинку-референс")
async def upload_image(
    current: Annotated[User, Depends(get_current_user)],
    assets: Annotated[AssetService, Depends(get_asset_service)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    file: Annotated[UploadFile, File()],
) -> AssetResponse:
    return await _store(
        file=file, kind=AssetKind.image, allowed=settings.upload_image_content_types,
        assets=assets, settings=settings, user=current,
    )
