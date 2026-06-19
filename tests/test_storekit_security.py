from __future__ import annotations

import jwt
import pytest

from app.api.errors import WebhookPayloadInvalid
from app.domain.providers.billing.apple import AppleStoreKitVerifier


def _fake_token() -> str:
    return jwt.encode(
        {"transactionId": "tx1", "productId": "com.musicfy.sub.weekly"},
        "attacker-key",
        algorithm="HS256",
    )


@pytest.mark.asyncio
async def test_verification_rejects_unsigned_token():
    """С включённой проверкой подписи поддельный токен без x5c отклоняется."""
    verifier = AppleStoreKitVerifier(bundle_id="com.musicfy.app", verify_signature=True)
    with pytest.raises(WebhookPayloadInvalid):
        verifier.decode_transaction(_fake_token())


@pytest.mark.asyncio
async def test_verification_rejects_bogus_x5c():
    """Токен с поддельной x5c-цепочкой отклоняется (untrusted root / bad cert)."""
    token = jwt.encode(
        {"transactionId": "tx1", "productId": "x"},
        "k",
        algorithm="HS256",
        headers={"x5c": ["bm90LWEtY2VydA==", "YWxzby1ub3QtYS1jZXJ0"]},
    )
    verifier = AppleStoreKitVerifier(bundle_id="com.musicfy.app", verify_signature=True)
    with pytest.raises(WebhookPayloadInvalid):
        verifier.decode_transaction(token)


@pytest.mark.asyncio
async def test_decode_only_mode_allows_synthetic_token():
    """В dev-режиме (verify_signature=False) синтетический токен декодируется."""
    verifier = AppleStoreKitVerifier(bundle_id="com.musicfy.app", verify_signature=False)
    tx = verifier.decode_transaction(_fake_token())
    assert tx["product_id"] == "com.musicfy.sub.weekly"


@pytest.mark.asyncio
async def test_verification_rejects_non_apple_root():
    """Валидная криптографически цепочка, но корень НЕ Apple Root CA - G3 → отказ.

    Проверяет, что путь верификации реально исполняет крипто-операции и пиннинг
    корня работает (а не просто падает на парсинге)."""
    import base64
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )
    from cryptography.x509.oid import NameOID

    def make_cert(cn, issuer_cert, issuer_key, key, is_ca):
        now = datetime.datetime.now(datetime.UTC)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
        return (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(issuer_cert.subject if issuer_cert else name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=is_ca, path_length=None), critical=True)
            .sign(issuer_key, hashes.SHA256())
        )

    root_key = ec.generate_private_key(ec.SECP256R1())
    root = make_cert("Fake Root", None, root_key, root_key, True)
    inter_key = ec.generate_private_key(ec.SECP256R1())
    inter = make_cert("Fake Intermediate", root, root_key, inter_key, True)
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = make_cert("Fake Leaf", inter, inter_key, leaf_key, False)

    x5c = [
        base64.b64encode(c.public_bytes(Encoding.DER)).decode()
        for c in (leaf, inter, root)
    ]
    leaf_pem = leaf_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    token = jwt.encode(
        {"transactionId": "tx1", "productId": "x"}, leaf_pem, algorithm="ES256",
        headers={"x5c": x5c},
    )
    verifier = AppleStoreKitVerifier(bundle_id="com.musicfy.app", verify_signature=True)
    with pytest.raises(WebhookPayloadInvalid) as exc:
        verifier.decode_transaction(token)
    assert exc.value.details["reason"] == "untrusted_root"
