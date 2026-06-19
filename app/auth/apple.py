from __future__ import annotations

import logging
import time
from typing import Any

import httpx
import jwt
from jwt import PyJWK, PyJWTError

from app.api.errors import AppleIdentityInvalid

logger = logging.getLogger(__name__)

APPLE_ISSUER = "https://appleid.apple.com"
APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
_JWKS_TTL_SECONDS = 3600


class AppleIdentityVerifier:
    """Верификация Apple identity token (Sign in with Apple).

    Загружает и кэширует JWKS Apple, проверяет подпись (RS256), issuer и audience.
    """

    def __init__(self, *, allowed_audiences: list[str]) -> None:
        self._allowed_audiences = allowed_audiences
        self._jwks: list[dict[str, Any]] = []
        self._jwks_fetched_at: float = 0.0
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get_jwks(self, *, now: float) -> list[dict[str, Any]]:
        if self._jwks and (now - self._jwks_fetched_at) < _JWKS_TTL_SECONDS:
            return self._jwks
        try:
            resp = await self._client.get(APPLE_JWKS_URL)
            resp.raise_for_status()
            keys = resp.json().get("keys", [])
        except (httpx.HTTPError, ValueError) as exc:
            if self._jwks:
                logger.warning("Apple JWKS refresh failed, using cached: %s", exc)
                return self._jwks
            raise AppleIdentityInvalid(details={"reason": "jwks_unavailable"}) from exc
        if not isinstance(keys, list) or not keys:
            raise AppleIdentityInvalid(details={"reason": "jwks_empty"})
        self._jwks = keys
        self._jwks_fetched_at = now
        return keys

    async def verify(self, identity_token: str, *, nonce: str | None = None) -> dict[str, Any]:
        """Проверяет токен и возвращает claims (sub, email, ...).

        `now` берётся из time.time() — допустимо: верификация в request-контексте.
        """
        if not self._allowed_audiences:
            raise AppleIdentityInvalid(details={"reason": "audience_not_configured"})
        now = time.time()
        try:
            header = jwt.get_unverified_header(identity_token)
        except PyJWTError as exc:
            raise AppleIdentityInvalid(details={"reason": "bad_header"}) from exc
        kid = header.get("kid")
        if not kid:
            raise AppleIdentityInvalid(details={"reason": "no_kid"})

        jwks = await self._get_jwks(now=now)
        jwk_dict = next((k for k in jwks if k.get("kid") == kid), None)
        if jwk_dict is None:
            # ключ мог обновиться — сбросим кэш и попробуем ещё раз
            self._jwks_fetched_at = 0.0
            jwks = await self._get_jwks(now=now)
            jwk_dict = next((k for k in jwks if k.get("kid") == kid), None)
        if jwk_dict is None:
            raise AppleIdentityInvalid(details={"reason": "kid_not_found"})

        try:
            signing_key = PyJWK.from_dict(jwk_dict).key
            claims = jwt.decode(
                identity_token,
                signing_key,
                algorithms=["RS256"],
                audience=self._allowed_audiences,
                issuer=APPLE_ISSUER,
            )
        except PyJWTError as exc:
            raise AppleIdentityInvalid(details={"reason": "verify_failed"}) from exc

        if nonce is not None and claims.get("nonce") != nonce:
            raise AppleIdentityInvalid(details={"reason": "nonce_mismatch"})

        if not claims.get("sub"):
            raise AppleIdentityInvalid(details={"reason": "no_sub"})
        return claims
