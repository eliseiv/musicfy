from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def to_camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )


class ErrorDetail(BaseModel):
    """Тело ошибки внутри обёртки `error`."""

    model_config = ConfigDict(populate_by_name=True)

    code: str = Field(
        description=(
            "Машинно-читаемый код ошибки в UPPER_SNAKE_CASE "
            "(INVALID_INPUT, SUBSCRIPTION_REQUIRED, INSUFFICIENT_CREDITS, ...)."
        ),
        examples=["INVALID_INPUT"],
    )
    message: str = Field(
        description="Человеко-читаемое описание ошибки.",
        examples=["Request validation failed"],
    )
    details: dict[str, Any] | None = Field(
        default=None,
        description="Дополнительные детали (опционально).",
    )


class ErrorResponse(BaseModel):
    """Формат ошибок `{"error": {...}, "requestId": "..."}`."""

    model_config = ConfigDict(populate_by_name=True)

    error: ErrorDetail
    request_id: str | None = Field(
        default=None,
        alias="requestId",
        description="ID запроса (тот же, что в заголовке `X-Request-Id`).",
        examples=["b5830b11dc4747d4b6b85217eff10177"],
    )
