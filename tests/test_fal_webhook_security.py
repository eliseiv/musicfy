from __future__ import annotations

import base64
import hashlib

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app.api.errors import WebhookSignatureInvalid
from app.domain.providers.fal.signature import (
    FAL_HDR_REQUEST_ID,
    FAL_HDR_SIGNATURE,
    FAL_HDR_TIMESTAMP,
    FAL_HDR_USER_ID,
    verify_fal_ed25519,
)


def _signed(body: bytes, *, ts: int, key: Ed25519PrivateKey):
    request_id, user_id = "req-1", "user-1"
    body_hash = hashlib.sha256(body).hexdigest()
    message = "\n".join([request_id, user_id, str(ts), body_hash]).encode()
    sig = key.sign(message).hex()
    headers = {
        FAL_HDR_REQUEST_ID: request_id,
        FAL_HDR_USER_ID: user_id,
        FAL_HDR_TIMESTAMP: str(ts),
        FAL_HDR_SIGNATURE: sig,
    }
    return headers


def _jwks(key: Ed25519PrivateKey) -> list[dict]:
    raw = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    x = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return [{"kty": "OKP", "crv": "Ed25519", "x": x}]


def test_valid_ed25519_signature_passes():
    key = Ed25519PrivateKey.generate()
    body = b'{"request_id":"req-1","status":"completed"}'
    headers = _signed(body, ts=1000, key=key)
    # не бросает
    verify_fal_ed25519(headers=headers, raw_body=body, jwk_keys=_jwks(key), now=1000)


def test_tampered_body_rejected():
    key = Ed25519PrivateKey.generate()
    headers = _signed(b"original", ts=1000, key=key)
    with pytest.raises(WebhookSignatureInvalid):
        verify_fal_ed25519(headers=headers, raw_body=b"tampered", jwk_keys=_jwks(key), now=1000)


def test_replay_old_timestamp_rejected():
    key = Ed25519PrivateKey.generate()
    body = b"x"
    headers = _signed(body, ts=1000, key=key)
    with pytest.raises(WebhookSignatureInvalid) as exc:
        verify_fal_ed25519(headers=headers, raw_body=body, jwk_keys=_jwks(key), now=99999)
    assert exc.value.details["reason"] == "timestamp_out_of_range"


def test_wrong_key_rejected():
    key = Ed25519PrivateKey.generate()
    other = Ed25519PrivateKey.generate()
    body = b"x"
    headers = _signed(body, ts=1000, key=key)
    with pytest.raises(WebhookSignatureInvalid):
        verify_fal_ed25519(headers=headers, raw_body=body, jwk_keys=_jwks(other), now=1000)
