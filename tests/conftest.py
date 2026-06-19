from __future__ import annotations

import os

import pytest_asyncio

# Тестовая БД (postgres из docker-compose на порту 5544). Переопределяется через env.
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://musicfy:musicfy@localhost:5544/musicfy"
)
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("API_KEY", "test-service-key")
os.environ.setdefault("ADMIN_API_KEY", "test-admin-key")
os.environ.setdefault("FAL_USE_STUB", "true")
os.environ.setdefault("FAL_WEBHOOK_SECRET", "test-webhook-secret")
os.environ.setdefault("APPLE_BUNDLE_ID", "com.musicfy.app")
# Тесты используют синтетические StoreKit-токены — без проверки подписи.
os.environ.setdefault("APPLE_STOREKIT_VERIFY_SIGNATURE", "false")

from asgi_lifespan import LifespanManager  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.session import build_engine, build_sessionmaker  # noqa: E402
from app.main import create_app  # noqa: E402

# Таблицы, очищаемые между тестами (по мере добавления фаз — дополнять).
# TRUNCATE ... CASCADE снимет зависимые строки; prompt_presets не трогаем (сид).
_TRUNCATE_TABLES = [
    "track_variants",
    "tracks",
    "job_stage_log",
    "jobs",
    "lyrics_drafts",
    "assets",
    "voice_profiles",
    "voice_consents",
    "device_push_tokens",
    "usage_events",
    "moderation_cases",
    "credit_ledger",
    "credit_balances",
    "entitlements",
    "purchases",
    "subscription_state",
    "sessions",
    "auth_identities",
    "processed_webhooks",
    "users",
]


@pytest_asyncio.fixture
async def app():
    # FAL_USE_STUB=true → дефолтная фабрика поднимает StubFalProvider.
    settings = get_settings()
    application = create_app(settings)
    async with LifespanManager(application):
        yield application


@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture(autouse=True)
async def clean_db():
    settings = get_settings()
    engine = build_engine(settings)
    sessionmaker = build_sessionmaker(engine)
    async with sessionmaker() as session:
        async with session.begin():
            for table in _TRUNCATE_TABLES:
                await session.execute(text(f"TRUNCATE TABLE {table} CASCADE"))
    await engine.dispose()
    yield
