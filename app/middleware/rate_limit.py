from __future__ import annotations

import asyncio
import hashlib
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.auth.api_keys import extract_bearer
from app.logging_config import request_id_var
from app.schemas.common import ErrorDetail, ErrorResponse


@dataclass
class TokenBucket:
    capacity: float
    refill_per_sec: float
    tokens: float
    last_refill: float

    def consume(self, now: float) -> tuple[bool, float]:
        elapsed = max(0.0, now - self.last_refill)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
        self.last_refill = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True, 0.0
        deficit = 1.0 - self.tokens
        retry_after = deficit / self.refill_per_sec if self.refill_per_sec > 0 else 60.0
        return False, retry_after


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Простой per-token rate limit. Ключ — bearer-токен (сессия или сервисный ключ)."""

    SKIP_PATHS = frozenset(
        {
            "/healthz",
            "/docs",
            "/redoc",
            "/openapi.json",
            "/v1/webhooks/fal",
            "/v1/webhooks/billing/apple",
        }
    )

    def __init__(self, app, *, per_minute: int, burst: int) -> None:
        super().__init__(app)
        self._capacity = float(max(1, burst))
        self._refill = float(per_minute) / 60.0
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = asyncio.Lock()

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        if path in self.SKIP_PATHS:
            return await call_next(request)

        token = extract_bearer(request.headers.get("authorization"))
        if not token:
            return await call_next(request)

        key = hashlib.sha256(token.encode("utf-8")).hexdigest()

        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = TokenBucket(
                    capacity=self._capacity,
                    refill_per_sec=self._refill,
                    tokens=self._capacity,
                    last_refill=time.monotonic(),
                )
                self._buckets[key] = bucket
            allowed, retry_after = bucket.consume(time.monotonic())

        if not allowed:
            retry = max(1, math.ceil(retry_after))
            body = ErrorResponse(
                error=ErrorDetail(
                    code="RATE_LIMITED",
                    message="Rate limit exceeded",
                    details={"retry_after_seconds": retry},
                ),
                requestId=request_id_var.get(),
            ).model_dump(by_alias=True, exclude_none=True)
            return JSONResponse(body, status_code=429, headers={"Retry-After": str(retry)})

        return await call_next(request)
