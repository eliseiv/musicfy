from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.api.errors import WebhookSignatureInvalid

# Dev/stub: HMAC по общему секрету.
SIGNATURE_HEADER = "X-Fal-Signature"

# Production fal: ED25519 + JWKS.
FAL_JWKS_URL = "https://rest.fal.ai/.well-known/jwks.json"
FAL_HDR_REQUEST_ID = "x-fal-webhook-request-id"
FAL_HDR_USER_ID = "x-fal-webhook-user-id"
FAL_HDR_TIMESTAMP = "x-fal-webhook-timestamp"
FAL_HDR_SIGNATURE = "x-fal-webhook-signature"
FAL_TIMESTAMP_TOLERANCE_SECONDS = 300


def compute_signature(secret: str, raw_body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def verify_signature(
    *, secret: str, raw_body: bytes, headers: Mapping[str, str]
) -> None:
    """HMAC-проверка (dev/stub)."""
    if not secret:
        raise WebhookSignatureInvalid(details={"reason": "secret_not_configured"})
    received = headers.get(SIGNATURE_HEADER) or headers.get(SIGNATURE_HEADER.lower())
    if not received:
        raise WebhookSignatureInvalid(details={"reason": "header_missing"})
    expected = compute_signature(secret, raw_body)
    if not hmac.compare_digest(received.strip(), expected):
        raise WebhookSignatureInvalid(details={"reason": "mismatch"})


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def has_fal_ed25519_headers(headers: Mapping[str, str]) -> bool:
    g = lambda k: headers.get(k) or headers.get(k.title())  # noqa: E731
    return bool(g(FAL_HDR_SIGNATURE) and g(FAL_HDR_TIMESTAMP) and g(FAL_HDR_REQUEST_ID))


def verify_fal_ed25519(
    *,
    headers: Mapping[str, str],
    raw_body: bytes,
    jwk_keys: list[dict],
    now: float,
) -> None:
    """Проверка подписи webhook'а fal (ED25519).

    message = request_id \\n user_id \\n timestamp \\n sha256_hex(body)
    Подпись (hex) проверяется против каждого публичного ключа из JWKS.
    """
    def get(k: str) -> str | None:
        return headers.get(k) or headers.get(k.title()) or headers.get(k.upper())

    request_id = get(FAL_HDR_REQUEST_ID)
    user_id = get(FAL_HDR_USER_ID)
    timestamp = get(FAL_HDR_TIMESTAMP)
    signature_hex = get(FAL_HDR_SIGNATURE)
    if not (request_id and timestamp and signature_hex):
        raise WebhookSignatureInvalid(details={"reason": "headers_missing"})

    # Анти-replay: timestamp в пределах ±tolerance.
    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise WebhookSignatureInvalid(details={"reason": "bad_timestamp"}) from exc
    if abs(now - ts) > FAL_TIMESTAMP_TOLERANCE_SECONDS:
        raise WebhookSignatureInvalid(details={"reason": "timestamp_out_of_range"})

    body_hash = hashlib.sha256(raw_body).hexdigest()
    message = "\n".join([request_id, user_id or "", timestamp, body_hash]).encode("utf-8")
    try:
        signature = bytes.fromhex(signature_hex.strip())
    except ValueError as exc:
        raise WebhookSignatureInvalid(details={"reason": "bad_signature_hex"}) from exc

    if not jwk_keys:
        raise WebhookSignatureInvalid(details={"reason": "no_public_keys"})
    for jwk in jwk_keys:
        x = jwk.get("x")
        if not x:
            continue
        try:
            pub = Ed25519PublicKey.from_public_bytes(_b64url_decode(x))
            pub.verify(signature, message)
            return  # успешная проверка
        except (InvalidSignature, ValueError):
            continue
    raise WebhookSignatureInvalid(details={"reason": "no_matching_key"})


def body_digest(raw_body: bytes) -> str:
    return hashlib.sha256(raw_body).hexdigest()
