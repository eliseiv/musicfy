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


async def job_input_payload(app, job_id: str) -> dict:
    """Возвращает сохранённый input_payload джобы (для проверки резолва target_voice)."""
    async with app.state.sessionmaker() as session:
        job = await session.get(Job, _uuid.UUID(job_id))
        return dict(job.input_payload or {})


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


async def emit_fal_demucs_completed(
    client, request_id: str, *, stems: dict[str, str],
):
    """Эмулирует РЕАЛЬНЫЙ demucs-конверт fal: стемы верхнеуровневыми ключами payload.

    fal demucs НЕ оборачивает результат в ``payload["stems"]`` — он кладёт каждый
    стем отдельным верхнеуровневым ключом: ``{"vocals":{"url":..},"drums":{"url":..},
    "bass":{"url":..},"other":{"url":..}}``. Именно этот формат разбирает
    ``extract_stems`` (demucs-путь, порог >=2). Старый код читал только
    ``result["stems"]`` и на этом формате вернул бы stems=None (регресс ADR-008).

    ``stems`` — маппинг имя_стема → url; кладём как ``{name: {"url": url}}``.
    """
    payload = {name: {"url": url} for name, url in stems.items()}
    body = json.dumps(
        {"request_id": request_id, "status": "OK", "payload": payload, "error": None}
    ).encode("utf-8")
    sig = compute_signature(WEBHOOK_SECRET, body)
    return await client.post(
        "/v1/webhooks/fal",
        content=body,
        headers={"X-Fal-Signature": sig, "Content-Type": "application/json"},
    )


async def emit_fal_error(client, request_id: str, *, error: str = "model inference failed"):
    # Реальный fal queue ERROR-конверт (TD-003): {request_id,status:"ERROR",error,payload}.
    # Парсер маппит ERROR → failed; webhook-route переводит job в failed и делает refund.
    body = json.dumps(
        {
            "request_id": request_id,
            "status": "ERROR",
            "error": error,
            "payload": {"detail": [{"loc": ["body"], "msg": "invalid"}]},
        }
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


ADMIN_HEADERS = {"Authorization": "Bearer test-admin-key"}


async def auth_headers(client) -> dict:
    token = (await client.post("/v1/auth/guest", json={})).json()["token"]
    return {"Authorization": f"Bearer {token}"}


async def auth_user(client) -> tuple[str, dict]:
    """Возвращает (user_id, headers) для нового гостевого пользователя."""
    r = (await client.post("/v1/auth/guest", json={})).json()
    return r["userId"], {"Authorization": f"Bearer {r['token']}"}


async def grant_coins(client, headers, coins: int = 100) -> None:
    """Начисляет монеты текущему пользователю (из headers) через admin /credits.

    Достаточно для любой генерации (song=10, cover=5, video=30) в E2E-тестах.
    """
    me = (await client.get("/v1/auth/me", headers=headers)).json()
    user_id = me["userId"]
    resp = await client.post(
        f"/v1/admin/users/{user_id}/credits",
        json={"coins": coins, "reason": "test grant"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200, resp.text


async def grant_weekly_subscription(client, headers) -> None:
    """Начисляет монеты текущему пользователю (совместимость с E2E-тестами).

    Раньше выдавала недельную подписку с per-category лимитами; в монетной модели
    просто пополняет единый кошелёк достаточным балансом для генераций.
    """
    await grant_coins(client, headers, coins=100)
