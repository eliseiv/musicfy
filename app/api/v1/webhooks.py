from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.deps import (
    get_billing_service,
    get_fal_provider,
    get_pipeline_runner,
    get_sessionmaker,
)
from app.domain.enums import JobStage, WebhookProvider
from app.domain.providers.fal.base import FalProvider
from app.domain.repositories.jobs import JobsRepository
from app.domain.repositories.webhooks import WebhooksRepository
from app.domain.services.billing_service import BillingService
from app.domain.services.pipelines.runner import PipelineRunner

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])


@router.post("/fal", summary="Webhook завершения fal-задачи")
async def fal_webhook(
    request: Request,
    fal: Annotated[FalProvider, Depends(get_fal_provider)],
    runner: Annotated[PipelineRunner, Depends(get_pipeline_runner)],
    sessionmaker: Annotated[object, Depends(get_sessionmaker)],
) -> dict:
    raw = await request.body()
    await fal.verify_webhook(headers=request.headers, raw_body=raw)
    event = fal.parse_webhook_event(headers=request.headers, raw_body=raw)

    # Phase 1: claim идемпотентности.
    async with sessionmaker() as session:
        async with session.begin():
            recorded = await WebhooksRepository(session).try_record(
                provider=WebhookProvider.fal,
                event_id=event.event_id,
                payload_digest=event.payload_digest,
            )
    if not recorded:
        return {"status": "duplicate"}

    # Находим job по provider_request_id.
    async with sessionmaker() as session:
        job = await JobsRepository(session).find_by_request_id(event.request_id)
        job_id = job.id if job else None
        current_stage = job.current_stage if job else None
    if job_id is None:
        logger.warning("fal webhook: no job for request_id=%s", event.request_id)
        return {"status": "ignored"}

    stage: JobStage = current_stage or JobStage.music_generation
    if event.status == "completed":
        await runner.advance(
            job_id=job_id,
            completed_stage=stage,
            media_url=event.media_url,
            duration_seconds=event.duration_seconds,
            stems=event.stems,
            event_id=event.event_id,
        )
    elif event.status in ("failed", "canceled"):
        await runner.fail(
            job_id=job_id,
            failed_stage=stage,
            error_code="PROVIDER_FAILED" if event.status == "failed" else "PROVIDER_CANCELED",
            error_message=event.error_message or event.status,
        )
    else:
        return {"status": "ack"}

    # Phase 2: applied.
    async with sessionmaker() as session:
        async with session.begin():
            await WebhooksRepository(session).mark_applied(
                provider=WebhookProvider.fal, event_id=event.event_id
            )
    return {"status": "ok"}


@router.post("/billing/apple", summary="App Store Server Notification V2")
async def apple_billing_webhook(
    request: Request,
    billing: Annotated[BillingService, Depends(get_billing_service)],
) -> dict:
    import json

    raw = await request.body()
    try:
        body = json.loads(raw.decode("utf-8"))
        signed_payload = body["signedPayload"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        from app.api.errors import WebhookPayloadInvalid

        raise WebhookPayloadInvalid(details={"reason": "no_signed_payload"}) from None
    return await billing.apply_notification(signed_payload=signed_payload)
