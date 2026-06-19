from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.logging_config import provider_var, request_id_var, user_id_var

logger = logging.getLogger("app.access")


class RequestContextMiddleware(BaseHTTPMiddleware):
    HEADER = "X-Request-Id"

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        incoming = request.headers.get(self.HEADER)
        request_id = incoming or uuid.uuid4().hex
        request.state.request_id = request_id
        rid_token = request_id_var.set(request_id)
        uid_token = user_id_var.set(None)
        prov_token = provider_var.set(None)

        start = time.perf_counter()
        response: Response | None = None
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers[self.HEADER] = request_id
            return response
        finally:
            latency_ms = round((time.perf_counter() - start) * 1000, 2)
            user_id = getattr(request.state, "user_id", None)
            if user_id is not None:
                user_id_var.set(str(user_id))
            logger.info(
                "request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": status_code,
                    "latency_ms": latency_ms,
                },
            )
            request_id_var.reset(rid_token)
            user_id_var.reset(uid_token)
            provider_var.reset(prov_token)
