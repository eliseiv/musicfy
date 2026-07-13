"""ADR-013 D3 — trust anchor выбирает окружение; глобального bypass в проде нет.

Два инварианта:
  * пин StoreKit Test root (`APPLE_STOREKIT_TEST_ROOT_CERTS`) → environment ПРИНУДИТЕЛЬНО
    `Xcode`, даже если claim лжёт `Production`; без пина тот же токен → untrusted_root;
  * регресс на P0-дыру: неподписанный самодельный JWS при verify_signature=true отклоняется
    (WebhookPayloadInvalid) до какого-либо начисления монет.
"""
from __future__ import annotations

import base64
import datetime

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from cryptography.x509.oid import NameOID

from app.api.errors import WebhookPayloadInvalid
from app.domain.enums import StoreKitEnvironment
from app.domain.providers.billing.apple import AppleStoreKitVerifier


def _make_cert(cn, issuer_cert, issuer_key, key, *, is_ca):
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


def _fake_storekit_chain():
    """Синтетическая цепочка leaf←intermediate←root (роль «StoreKit Test» root)."""
    root_key = ec.generate_private_key(ec.SECP256R1())
    root = _make_cert("StoreKit Testing Root", None, root_key, root_key, is_ca=True)
    inter_key = ec.generate_private_key(ec.SECP256R1())
    inter = _make_cert("StoreKit Testing CA", root, root_key, inter_key, is_ca=True)
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = _make_cert("StoreKit Leaf", inter, inter_key, leaf_key, is_ca=False)
    x5c = [
        base64.b64encode(c.public_bytes(Encoding.DER)).decode() for c in (leaf, inter, root)
    ]
    root_pem = root.public_bytes(Encoding.PEM).decode()
    leaf_pem = leaf_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    return root_pem, leaf_pem, x5c


def _signed_by_chain(leaf_pem: bytes, x5c: list[str], claims: dict) -> str:
    return jwt.encode(claims, leaf_pem, algorithm="ES256", headers={"x5c": x5c})


# --------------------------------------------------------------------------
# D3: пин StoreKit Test root → environment принудительно Xcode
# --------------------------------------------------------------------------


def test_pinned_test_root_forces_xcode_even_if_claim_lies_production():
    """Корень цепочки закреплён как StoreKit Test → environment=Xcode, claim игнорируется."""
    root_pem, leaf_pem, x5c = _fake_storekit_chain()
    # claim нагло заявляет Production — не должно повлиять при пине тестового корня
    token = _signed_by_chain(
        leaf_pem, x5c, {"transactionId": "0", "productId": "x", "environment": "Production"}
    )
    verifier = AppleStoreKitVerifier(
        bundle_id="com.musicfy.app", verify_signature=True, test_root_certs_pem=[root_pem]
    )
    tx = verifier.decode_transaction(token)
    assert tx["environment"] == StoreKitEnvironment.xcode.value


def test_same_token_without_pin_is_untrusted_root():
    """Тот же токен без пина → untrusted_root (Xcode-ветка выключена по умолчанию)."""
    root_pem, leaf_pem, x5c = _fake_storekit_chain()
    token = _signed_by_chain(
        leaf_pem, x5c, {"transactionId": "0", "productId": "x", "environment": "Production"}
    )
    verifier = AppleStoreKitVerifier(bundle_id="com.musicfy.app", verify_signature=True)
    with pytest.raises(WebhookPayloadInvalid) as exc:
        verifier.decode_transaction(token)
    assert exc.value.details["reason"] == "untrusted_root"


# --------------------------------------------------------------------------
# D3: регресс на P0-дыру — неподписанный самодельный JWS отклоняется
# --------------------------------------------------------------------------


def test_unsigned_forged_token_rejected_when_verifying():
    """verify_signature=true: самодельный JWS (без x5c) на дорогой продукт → отказ, не грант."""
    forged = jwt.encode(
        {"transactionId": "free-money", "productId": "com.musicfy.coins.large"},
        "attacker-key",
        algorithm="HS256",
    )
    verifier = AppleStoreKitVerifier(bundle_id="com.musicfy.app", verify_signature=True)
    with pytest.raises(WebhookPayloadInvalid) as exc:
        verifier.decode_transaction(forged)
    # payload отклонён на этапе верификации → до начисления монет дело не доходит
    assert exc.value.details["reason"] in {"no_x5c", "not_jws"}
