from __future__ import annotations

import types

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from pydantic import SecretStr

from app.domain.services.notification_service import NotificationService


def test_apns_jwt_is_valid_es256():
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    settings = types.SimpleNamespace(
        APNS_TEAM_ID="TEAM123456",
        APNS_KEY_ID="KEY7890AB",
        APNS_PRIVATE_KEY=SecretStr(pem),
    )
    svc = NotificationService(sessionmaker=None, settings=settings)  # type: ignore[arg-type]
    token = svc._apns_jwt()

    header = jwt.get_unverified_header(token)
    assert header["alg"] == "ES256"
    assert header["kid"] == "KEY7890AB"

    pub_pem = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    claims = jwt.decode(token, pub_pem, algorithms=["ES256"])
    assert claims["iss"] == "TEAM123456"
    assert "iat" in claims

    # повторный вызов в пределах TTL возвращает тот же токен (кэш)
    assert svc._apns_jwt() == token
