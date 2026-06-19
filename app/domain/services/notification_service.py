from __future__ import annotations

import logging
import time
from uuid import UUID

import httpx
import jwt
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.domain.models.device import DevicePushToken

logger = logging.getLogger(__name__)

APNS_PROD_HOST = "https://api.push.apple.com"
APNS_SANDBOX_HOST = "https://api.sandbox.push.apple.com"
# APNs-токен (provider JWT) можно переиспользовать ~20–60 мин; обновляем каждые 40.
_APNS_JWT_TTL = 2400


class NotificationService:
    """APNs push о завершении долгих задач.

    token-based аутентификация (ES256 JWT с .p8-ключом), HTTP/2 на api.push.apple.com.
    При APNS_ENABLED=false уведомления только логируются.
    """

    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], settings: Settings
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._client: httpx.AsyncClient | None = None
        self._jwt: str | None = None
        self._jwt_at: float = 0.0

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def notify_job_done(
        self, *, user_id: UUID, title: str, body: str, payload: dict | None = None
    ) -> None:
        async with self._sessionmaker() as session:
            stmt = select(DevicePushToken.token).where(DevicePushToken.user_id == user_id)
            tokens = list((await session.execute(stmt)).scalars().all())
        if not tokens:
            return
        for token in tokens:
            await self._send(token=token, title=title, body=body, payload=payload or {})

    # ----------------------------------------------------------------- private

    def _apns_jwt(self) -> str:
        now = time.time()
        if self._jwt and (now - self._jwt_at) < _APNS_JWT_TTL:
            return self._jwt
        self._jwt = jwt.encode(
            {"iss": self._settings.APNS_TEAM_ID, "iat": int(now)},
            self._settings.APNS_PRIVATE_KEY.get_secret_value(),
            algorithm="ES256",
            headers={"kid": self._settings.APNS_KEY_ID},
        )
        self._jwt_at = now
        return self._jwt

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            host = APNS_SANDBOX_HOST if self._settings.APNS_USE_SANDBOX else APNS_PROD_HOST
            self._client = httpx.AsyncClient(http2=True, base_url=host, timeout=10.0)
        return self._client

    async def _send(self, *, token: str, title: str, body: str, payload: dict) -> None:
        if not self._settings.APNS_ENABLED:
            logger.info("APNs (disabled) → token=%s title=%s", token[:8], title)
            return
        if not (
            self._settings.APNS_KEY_ID
            and self._settings.APNS_TEAM_ID
            and self._settings.APNS_TOPIC
            and self._settings.APNS_PRIVATE_KEY.get_secret_value()
        ):
            logger.warning("APNs enabled but credentials incomplete — skipping push")
            return
        aps = {"aps": {"alert": {"title": title, "body": body}, "sound": "default"}, **payload}
        headers = {
            "authorization": f"bearer {self._apns_jwt()}",
            "apns-topic": self._settings.APNS_TOPIC,
            "apns-push-type": "alert",
            "apns-priority": "10",
        }
        try:
            resp = await self._get_client().post(
                f"/3/device/{token}", json=aps, headers=headers
            )
        except httpx.HTTPError as exc:
            logger.warning("APNs send failed (%s): %s", token[:8], exc)
            return
        if resp.status_code == 200:
            return
        if resp.status_code == 410:
            # Токен больше не зарегистрирован — удаляем.
            logger.info("APNs 410 unregistered → removing token %s", token[:8])
            await self._delete_token(token)
            return
        logger.warning("APNs %s for token=%s: %s", resp.status_code, token[:8], resp.text[:200])

    async def _delete_token(self, token: str) -> None:
        try:
            async with self._sessionmaker() as session:
                async with session.begin():
                    await session.execute(
                        delete(DevicePushToken).where(DevicePushToken.token == token)
                    )
        except Exception:
            logger.exception("failed to delete stale push token")
