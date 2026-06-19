from __future__ import annotations

import json
import uuid as _uuid

import jwt

from app.domain.models.job import Job
from app.domain.providers.fal.signature import compute_signature

WEBHOOK_SECRET = "test-webhook-secret"


async def provider_request_id(app, job_id: str) -> str:
    async with app.state.sessionmaker() as session:
        job = await session.get(Job, _uuid.UUID(job_id))
        return job.provider_request_id


async def emit_fal_completed(
    client, request_id: str, *, media_url: str | None = None,
    duration: float | None = None, stems: dict | None = None,
):
    # Реальный fal queue webhook — конверт {request_id,status,payload,error},
    # где payload = результат модели ({"audio": {"url": .., "duration": ..}}).
    # duration модель не возвращает отдельно — кладём внутрь audio-объекта.
    result: dict = {}
    if media_url is not None:
        audio: dict = {"url": media_url}
        if duration is not None:
            audio["duration"] = duration
        result["audio"] = audio
    if stems is not None:
        result["stems"] = stems
    body = json.dumps(
        {"request_id": request_id, "status": "OK", "payload": result, "error": None}
    ).encode("utf-8")
    sig = compute_signature(WEBHOOK_SECRET, body)
    return await client.post(
        "/v1/webhooks/fal",
        content=body,
        headers={"X-Fal-Signature": sig, "Content-Type": "application/json"},
    )


def make_signed_transaction(
    *,
    product_id: str,
    transaction_id: str,
    original_transaction_id: str | None = None,
    expires_date_ms: int | None = None,
    tx_type: str = "Auto-Renewable Subscription",
) -> str:
    """Крафтит JWS-подобный токен транзакции StoreKit для тестов.

    Verifier в V1 декодирует без проверки подписи, поэтому HS256-токена достаточно.
    """
    claims = {
        "transactionId": transaction_id,
        "originalTransactionId": original_transaction_id or transaction_id,
        "productId": product_id,
        "type": tx_type,
    }
    if expires_date_ms is not None:
        claims["expiresDate"] = expires_date_ms
    return jwt.encode(claims, "test-key", algorithm="HS256")


async def auth_headers(client) -> dict:
    token = (await client.post("/v1/auth/guest", json={})).json()["token"]
    return {"Authorization": f"Bearer {token}"}


async def grant_weekly_subscription(client, headers) -> None:
    """Выдаёт недельную подписку текущему пользователю через purchases/verify."""
    import time

    expires_ms = int((time.time() + 7 * 86400) * 1000)
    signed = make_signed_transaction(
        product_id="com.musicfy.sub.weekly",
        transaction_id=f"tx-{headers['Authorization'][-8:]}",
        expires_date_ms=expires_ms,
    )
    resp = await client.post(
        "/v1/billing/purchases/verify", json={"signedTransaction": signed}, headers=headers
    )
    assert resp.status_code == 200, resp.text
