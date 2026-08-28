"""Вебхук подписок Adapty: `POST /v1/billing/adapty/webhook` (ADR-019).

Вызывает Adapty (server-to-server), НЕ iOS-клиент. Авторизация — статический bearer через
per-route зависимость `require_adapty_webhook`, изолированную от пользовательских сессий и
админ-ключа. Тело читается СЫРЫМ, без Pydantic-модели запроса: кривой проверочный пинг обязан
получить 2xx, иначе Adapty не сохранит конфигурацию вебхука, а 422 он ретраил бы бесконечно.

Любой авторизованный исход — HTTP 200. 500 отдаётся только в двух случаях: секрет не задан
(конфигурационная ошибка) и реальный сбой БД — тогда Adapty ретраит, и переобработка чистая.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.deps import get_adapty_webhook_service, require_adapty_webhook
from app.domain.schemas.billing import AdaptyWebhookResponse
from app.domain.services.adapty_webhook_service import AdaptyWebhookService

router = APIRouter(prefix="/billing/adapty", tags=["Биллинг (Adapty)"])


@router.post(
    "/webhook",
    response_model=AdaptyWebhookResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(require_adapty_webhook)],
    summary="Вебхук подписок Adapty",
    responses={
        401: {"description": "Отсутствует или неверный bearer-секрет"},
        500: {"description": "Секрет не сконфигурирован либо сбой БД (Adapty ретраит)"},
    },
)
async def adapty_webhook(
    request: Request,
    service: Annotated[AdaptyWebhookService, Depends(get_adapty_webhook_service)],
) -> AdaptyWebhookResponse:
    raw = await request.body()
    outcome = await service.handle(raw)
    return AdaptyWebhookResponse(
        result=outcome.result,
        reason=outcome.reason,
        event_type=outcome.event_type,
    )
