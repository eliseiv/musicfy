from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.logging_config import request_id_var
from app.schemas.common import ErrorDetail, ErrorResponse

logger = logging.getLogger(__name__)


class APIError(Exception):
    code: str = "ERROR"
    http_status: int = 500
    message: str = "Internal error"

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        code: str | None = None,
        http_status: int | None = None,
    ) -> None:
        if message is not None:
            self.message = message
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        self.details = details
        super().__init__(self.message)


# --- Generic ---


class ValidationFailed(APIError):
    code = "INVALID_INPUT"
    http_status = 400
    message = "Validation failed"


class RateLimited(APIError):
    code = "RATE_LIMITED"
    http_status = 429
    message = "Rate limit exceeded"


class AuthError(APIError):
    code = "UNAUTHORIZED"
    http_status = 401
    message = "Invalid or missing credentials"


class Forbidden(APIError):
    code = "FORBIDDEN"
    http_status = 403
    message = "Resource belongs to another user"


# --- Auth / session ---


class InvalidSession(APIError):
    code = "INVALID_SESSION"
    http_status = 401
    message = "Session token is invalid or expired"


class AppleIdentityInvalid(APIError):
    code = "APPLE_IDENTITY_INVALID"
    http_status = 401
    message = "Apple identity token verification failed"


# --- Subscription / credits ---


class SubscriptionRequired(APIError):
    code = "SUBSCRIPTION_REQUIRED"
    http_status = 402
    message = "Active subscription required"


class SubscriptionExpired(SubscriptionRequired):
    code = "SUBSCRIPTION_EXPIRED"
    http_status = 402
    message = "Subscription has expired"


class InsufficientCredits(APIError):
    code = "INSUFFICIENT_CREDITS"
    http_status = 402
    message = "Not enough generation credits to perform the operation"


# --- Resources ---


class UserNotFound(APIError):
    code = "USER_NOT_FOUND"
    http_status = 404
    message = "User not found"


class JobNotFound(APIError):
    code = "JOB_NOT_FOUND"
    http_status = 404
    message = "Generation job not found"


class TrackNotFound(APIError):
    code = "TRACK_NOT_FOUND"
    http_status = 404
    message = "Track not found"


class AssetNotFound(APIError):
    code = "ASSET_NOT_FOUND"
    http_status = 404
    message = "Asset not found"


class VoiceProfileNotFound(APIError):
    code = "VOICE_PROFILE_NOT_FOUND"
    http_status = 404
    message = "Voice profile not found"


class VideoNotFound(APIError):
    code = "VIDEO_NOT_FOUND"
    http_status = 404
    message = "Video not found"


class LyricsDraftNotFound(APIError):
    code = "LYRICS_DRAFT_NOT_FOUND"
    http_status = 404
    message = "Lyrics draft not found"


class PresetNotFound(APIError):
    code = "PRESET_NOT_FOUND"
    http_status = 404
    message = "Preset not found"


# --- Validation / consent / moderation ---


class InvalidAssetUrl(APIError):
    code = "INVALID_ASSET_URL"
    http_status = 400
    message = "Asset URL is not reachable or not allowed"


class UploadRejected(APIError):
    code = "UPLOAD_REJECTED"
    http_status = 400
    message = "Uploaded file is too large or has an unsupported content type"


class ConsentRequired(APIError):
    code = "CONSENT_REQUIRED"
    http_status = 403
    message = "Voice consent is required before this operation"


class ModerationBlocked(APIError):
    code = "MODERATION_BLOCKED"
    http_status = 422
    message = "Content was blocked by moderation"


# --- Webhooks ---


class WebhookSignatureInvalid(APIError):
    code = "WEBHOOK_SIGNATURE_INVALID"
    http_status = 401
    message = "Webhook signature verification failed"


class WebhookPayloadInvalid(APIError):
    code = "WEBHOOK_PAYLOAD_INVALID"
    http_status = 400
    message = "Webhook payload is malformed"


# --- Internal / providers ---


class PricingRuleMissing(APIError):
    code = "PRICING_RULE_MISSING"
    http_status = 500
    message = "No active pricing rule configured for the provider model"


class FalProviderError(APIError):
    code = "PROVIDER_FAILED"
    http_status = 502
    message = "fal.ai provider returned an error"


class FalTimeout(APIError):
    code = "PROVIDER_TIMEOUT"
    http_status = 504
    message = "fal.ai provider timed out"


def _envelope(
    *,
    code: str,
    message: str,
    status_code: int,
    details: dict[str, Any] | None,
    request_id: str | None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorDetail(code=code, message=message, details=details),
        requestId=request_id,
    ).model_dump(by_alias=True, exclude_none=True)
    return JSONResponse(body, status_code=status_code, headers=headers)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def api_error_handler(request: Request, exc: APIError) -> JSONResponse:
        if exc.http_status >= 500:
            logger.exception("API error: %s", exc.message)
        else:
            logger.info("API error: %s (%s)", exc.code, exc.message)
        headers: dict[str, str] | None = None
        if isinstance(exc, RateLimited) and exc.details:
            retry_after = exc.details.get("retry_after_seconds")
            if retry_after is not None:
                headers = {"Retry-After": str(int(retry_after))}
        return _envelope(
            code=exc.code,
            message=exc.message,
            status_code=exc.http_status,
            details=exc.details,
            request_id=request_id_var.get(),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # jsonable_encoder normalises non-serialisable values in errors()
        # (e.g. ctx={'error': ValueError(...)} from pydantic model_validator),
        # so building JSONResponse can't raise TypeError -> real 400 INVALID_INPUT.
        return _envelope(
            code="INVALID_INPUT",
            message="Request validation failed",
            status_code=400,
            details={"errors": jsonable_encoder(exc.errors())},
            request_id=request_id_var.get(),
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception")
        return _envelope(
            code="INTERNAL_ERROR",
            message="Internal server error",
            status_code=500,
            details=None,
            request_id=request_id_var.get(),
        )
