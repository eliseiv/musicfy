from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    admin,
    analytics,
    auth,
    billing,
    covers,
    devices,
    jobs,
    library,
    lyrics,
    presets,
    songs,
    tracks,
    uploads,
    videos,
    voices,
    webhooks,
)

api_v1_router = APIRouter(prefix="/v1")
api_v1_router.include_router(auth.router)
api_v1_router.include_router(presets.router)
api_v1_router.include_router(lyrics.router)
api_v1_router.include_router(songs.router)
api_v1_router.include_router(covers.router)
api_v1_router.include_router(uploads.router)
api_v1_router.include_router(voices.router)
api_v1_router.include_router(videos.router)
api_v1_router.include_router(devices.router)
api_v1_router.include_router(jobs.router)
api_v1_router.include_router(tracks.router)
api_v1_router.include_router(library.router)
api_v1_router.include_router(billing.router)
api_v1_router.include_router(analytics.router)
api_v1_router.include_router(admin.router)
api_v1_router.include_router(webhooks.router)
