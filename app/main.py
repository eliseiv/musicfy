from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.errors import register_exception_handlers
from app.api.openapi import API_DESCRIPTION, OPENAPI_TAGS
from app.api.v1.router import api_v1_router
from app.auth.apple import AppleIdentityVerifier
from app.auth.sessions import AuthService
from app.config import Settings, get_settings
from app.db.session import build_engine, build_sessionmaker
from app.domain.providers.billing.apple import AppleStoreKitVerifier
from app.domain.providers.fal.client import FalAiProvider
from app.domain.providers.fal.stub import StubFalProvider
from app.domain.services.admin_service import AdminService
from app.domain.services.analytics_service import AnalyticsService
from app.domain.services.asset_service import AssetService
from app.domain.services.billing_service import BillingService
from app.domain.services.credits import CoinWalletService
from app.domain.services.generation_service import GenerationService
from app.domain.services.lyrics_service import LyricsService
from app.domain.services.merge import register as register_merge_reassigners
from app.domain.services.moderation_service import ModerationService
from app.domain.services.notification_service import NotificationService
from app.domain.services.pipelines.runner import PipelineRunner
from app.domain.services.poller import FalPoller
from app.domain.services.recovery import recover_orphan_jobs, report_received_webhooks
from app.logging_config import setup_logging
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.request_context import RequestContextMiddleware
from app.middleware.security_headers import SecurityHeadersMiddleware

logger = logging.getLogger(__name__)


def _default_fal_factory(settings: Settings):
    if settings.FAL_USE_STUB:
        logger.warning("FAL_USE_STUB=true — using in-process StubFalProvider (dev only)")
        return StubFalProvider(
            webhook_secret=settings.FAL_WEBHOOK_SECRET.get_secret_value(),
            video_lyrics_bg_model=settings.FAL_VIDEO_LYRICS_BG_MODEL,
            voice_conversion_model=settings.FAL_VOICE_CONVERSION_MODEL,
        )
    key = settings.FAL_API_KEY.get_secret_value()
    if not key:
        logger.warning("FAL_API_KEY is not configured; generation endpoints will 503")
        return None
    return FalAiProvider(
        api_key=key,
        base_url=settings.FAL_BASE_URL,
        song_model=settings.FAL_SONG_MODEL,
        refine_model=settings.FAL_REFINE_MODEL,
        speech_model=settings.FAL_SPEECH_MODEL,
        voice_clone_model=settings.FAL_VOICE_CLONE_MODEL,
        lyrics_llm=settings.FAL_LYRICS_LLM,
        demucs_model=settings.FAL_DEMUCS_MODEL,
        voice_changer_model=settings.FAL_VOICE_CHANGER_MODEL,
        voice_conversion_model=settings.FAL_VOICE_CONVERSION_MODEL,
        video_avatar_model=settings.FAL_VIDEO_AVATAR_MODEL,
        video_avatar_image_model=settings.FAL_VIDEO_AVATAR_IMAGE_MODEL,
        video_visual_model=settings.FAL_VIDEO_VISUAL_MODEL,
        video_visual_image_model=settings.FAL_VIDEO_VISUAL_IMAGE_MODEL,
        video_lyrics_bg_model=settings.FAL_VIDEO_LYRICS_BG_MODEL,
        webhook_secret=settings.FAL_WEBHOOK_SECRET.get_secret_value(),
        timeout_seconds=settings.FAL_HTTP_TIMEOUT_SECONDS,
    )


def create_app(
    settings: Settings | None = None,
    *,
    fal_factory=None,
    sessionmaker=None,
    engine=None,
) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings.LOG_LEVEL)
    register_merge_reassigners()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if engine is None and sessionmaker is None:
            local_engine = build_engine(settings)
            app.state.engine = local_engine
            app.state.sessionmaker = build_sessionmaker(local_engine)
        else:
            if engine is not None:
                app.state.engine = engine
            if sessionmaker is not None:
                app.state.sessionmaker = sessionmaker

        fal = (fal_factory or _default_fal_factory)(settings)
        app.state.fal_provider = fal

        apple_verifier = AppleIdentityVerifier(
            allowed_audiences=settings.apple_allowed_audiences
        )
        app.state.apple_verifier = apple_verifier
        app.state.auth_service = AuthService(
            app.state.sessionmaker,
            apple_verifier=apple_verifier,
            session_ttl_seconds=settings.SESSION_TTL_SECONDS,
        )

        # Кредиты / биллинг.
        credits = CoinWalletService(app.state.sessionmaker)
        app.state.credits_service = credits
        app.state.billing_service = BillingService(
            app.state.sessionmaker,
            verifier=AppleStoreKitVerifier(
                bundle_id=settings.APPLE_STOREKIT_BUNDLE_ID or settings.APPLE_BUNDLE_ID,
                verify_signature=settings.APPLE_STOREKIT_VERIFY_SIGNATURE,
                test_root_certs_pem=settings.apple_storekit_test_root_certs,
            ),
        )

        notifier = NotificationService(app.state.sessionmaker, settings)
        app.state.notification_service = notifier
        moderation = ModerationService(app.state.sessionmaker)
        analytics = AnalyticsService(app.state.sessionmaker)
        app.state.moderation_service = moderation
        app.state.analytics_service = analytics
        app.state.admin_service = AdminService(app.state.sessionmaker)

        # Pipeline / generation.
        poller = None
        if fal is not None:
            runner = PipelineRunner(
                app.state.sessionmaker, fal, settings, credits=credits, notifier=notifier
            )
            app.state.pipeline_runner = runner
            app.state.generation_service = GenerationService(
                app.state.sessionmaker, runner, settings, credits=credits,
                moderation=moderation, analytics=analytics,
            )
            app.state.lyrics_service = LyricsService(
                app.state.sessionmaker, fal, credits=credits
            )
            app.state.asset_service = AssetService(app.state.sessionmaker, fal)

            try:
                recovered = await recover_orphan_jobs(
                    sessionmaker=app.state.sessionmaker, credits=credits
                )
                if recovered:
                    logger.info("Recovered %d orphan jobs on startup", recovered)
                stuck = await report_received_webhooks(sessionmaker=app.state.sessionmaker)
                if stuck:
                    logger.warning("%d webhooks stuck in 'received'", stuck)
            except Exception:
                logger.exception("Recovery sweep failed on startup")

            if settings.POLL_ENABLED:
                poller = FalPoller(
                    sessionmaker=app.state.sessionmaker,
                    fal=fal,
                    runner=runner,
                    settings=settings,
                )
                poller.start()
                app.state.poller = poller

        try:
            yield
        finally:
            if poller is not None:
                try:
                    await poller.stop()
                except Exception:
                    logger.exception("Failed to stop FalPoller")
            notifier_inst = getattr(app.state, "notification_service", None)
            if notifier_inst is not None:
                try:
                    await notifier_inst.aclose()
                except Exception:
                    logger.exception("Failed to close notification service")
            verifier = getattr(app.state, "apple_verifier", None)
            if verifier is not None:
                try:
                    await verifier.aclose()
                except Exception:
                    logger.exception("Failed to close Apple verifier")
            fal_instance = getattr(app.state, "fal_provider", None)
            if fal_instance is not None and hasattr(fal_instance, "aclose"):
                try:
                    await fal_instance.aclose()
                except Exception:
                    logger.exception("Failed to close fal provider")
            local_engine = getattr(app.state, "engine", None)
            if local_engine is not None and engine is None:
                await local_engine.dispose()

    servers = []
    if settings.PUBLIC_BASE_URL:
        servers.append({"url": settings.PUBLIC_BASE_URL, "description": "Configured"})
    servers.append({"url": "http://localhost:8000", "description": "Local"})

    app = FastAPI(
        title="Musicfy API",
        description=API_DESCRIPTION,
        version="1.0.0",
        lifespan=lifespan,
        openapi_tags=OPENAPI_TAGS,
        servers=servers,
        contact={"name": "Musicfy Backend"},
        swagger_ui_parameters={"docExpansion": "none", "tagsSorter": "alpha", "filter": True},
    )

    app.state.settings = settings

    register_exception_handlers(app)
    if settings.RATE_LIMIT_PER_MINUTE > 0:
        app.add_middleware(
            RateLimitMiddleware,
            per_minute=settings.RATE_LIMIT_PER_MINUTE,
            burst=max(settings.RATE_LIMIT_BURST, 1),
        )
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        SecurityHeadersMiddleware, enable_hsts=(settings.APP_ENV == "prod")
    )

    app.include_router(api_v1_router)

    @app.get("/healthz", tags=["Система"], summary="Healthcheck")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
