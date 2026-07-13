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


def _xcode_single_cert():
    """ОДИН self-signed EC-cert — реальный формат Xcode StoreKit local testing.

    Xcode подписывает транзакции единственным self-signed сертификатом (subject==issuer),
    поэтому x5c имеет длину 1 и этот cert одновременно и leaf, и trust anchor.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    cert = _make_cert("StoreKit Testing (Xcode)", None, key, key, is_ca=False)
    cert_pem = cert.public_bytes(Encoding.PEM).decode()
    key_pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    x5c = [base64.b64encode(cert.public_bytes(Encoding.DER)).decode()]
    return cert_pem, key_pem, x5c


def _two_cert_chain():
    """Боевой путь: цепочка leaf←root длины 2 (root в роли pinned StoreKit Test root)."""
    root_key = ec.generate_private_key(ec.SECP256R1())
    root = _make_cert("StoreKit Testing Root", None, root_key, root_key, is_ca=True)
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = _make_cert("StoreKit Leaf", root, root_key, leaf_key, is_ca=False)
    x5c = [base64.b64encode(c.public_bytes(Encoding.DER)).decode() for c in (leaf, root)]
    root_pem = root.public_bytes(Encoding.PEM).decode()
    leaf_pem = leaf_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    return root_pem, leaf_pem, x5c


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


# --------------------------------------------------------------------------
# P1-регресс: Xcode local testing подписывает ОДНИМ self-signed cert (x5c длины 1).
# Прод-баг: код требовал len(x5c)>=2 → валидные Xcode-покупки отбивались как no_x5c.
# --------------------------------------------------------------------------


def test_xcode_single_self_signed_cert_happy_path():
    """x5c длины 1 (single self-signed cert), cert запинен → verify=True принимает покупку.

    Регресс на прод-баг: реальный формат Xcode (subject==issuer, x5c длины 1) обязан
    проходить верификацию и принудительно давать environment=Xcode.
    """
    cert_pem, key_pem, x5c = _xcode_single_cert()
    assert len(x5c) == 1  # ровно формат Xcode, не искусственная 2-cert цепочка
    # claim лжёт Production — при пине тестового корня должно игнорироваться
    token = _signed_by_chain(
        key_pem,
        x5c,
        {
            "transactionId": "42",
            "productId": "com.musicfy.coins.small",
            "environment": "Production",
        },
    )
    verifier = AppleStoreKitVerifier(
        bundle_id="com.musicfy.app", verify_signature=True, test_root_certs_pem=[cert_pem]
    )
    tx = verifier.decode_transaction(token)
    assert tx["environment"] == StoreKitEnvironment.xcode.value
    assert tx["transaction_id"] == "42"
    assert tx["product_id"] == "com.musicfy.coins.small"


def test_xcode_single_cert_via_verify_and_decode_returns_forced_xcode():
    """Нижнеуровневый _verify_and_decode для single-cert: claims + forced=Xcode."""
    cert_pem, key_pem, x5c = _xcode_single_cert()
    token = _signed_by_chain(key_pem, x5c, {"transactionId": "9", "productId": "p"})
    verifier = AppleStoreKitVerifier(
        bundle_id="com.musicfy.app", verify_signature=True, test_root_certs_pem=[cert_pem]
    )
    claims, forced = verifier._verify_and_decode(token)
    assert claims["transactionId"] == "9"
    assert forced == StoreKitEnvironment.xcode


def test_rogue_single_cert_not_pinned_is_untrusted_root():
    """Single self-signed cert, которого НЕТ в пине → untrusted_root. Защита не ослаблена."""
    _cert_pem, key_pem, x5c = _xcode_single_cert()
    token = _signed_by_chain(key_pem, x5c, {"transactionId": "1", "productId": "x"})
    verifier = AppleStoreKitVerifier(bundle_id="com.musicfy.app", verify_signature=True)
    with pytest.raises(WebhookPayloadInvalid) as exc:
        verifier.decode_transaction(token)
    assert exc.value.details["reason"] == "untrusted_root"


def test_empty_x5c_rejected_no_x5c():
    """x5c=[] → no_x5c (фикс допускает len>=1, но не пустоту)."""
    key = ec.generate_private_key(ec.SECP256R1())
    key_pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    token = jwt.encode(
        {"transactionId": "1", "productId": "x"}, key_pem, algorithm="ES256", headers={"x5c": []}
    )
    verifier = AppleStoreKitVerifier(bundle_id="com.musicfy.app", verify_signature=True)
    with pytest.raises(WebhookPayloadInvalid) as exc:
        verifier.decode_transaction(token)
    assert exc.value.details["reason"] == "no_x5c"


def test_missing_x5c_header_rejected_no_x5c():
    """Заголовок без x5c → no_x5c."""
    key = ec.generate_private_key(ec.SECP256R1())
    key_pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    token = jwt.encode({"transactionId": "1", "productId": "x"}, key_pem, algorithm="ES256")
    verifier = AppleStoreKitVerifier(bundle_id="com.musicfy.app", verify_signature=True)
    with pytest.raises(WebhookPayloadInvalid) as exc:
        verifier.decode_transaction(token)
    assert exc.value.details["reason"] == "no_x5c"


def test_multi_cert_chain_still_verifies_after_fix():
    """Боевой путь: валидная 2-cert цепочка leaf←root продолжает верифицироваться (не сломана)."""
    root_pem, leaf_pem, x5c = _two_cert_chain()
    assert len(x5c) >= 2
    token = _signed_by_chain(
        leaf_pem,
        x5c,
        {"transactionId": "7", "productId": "com.musicfy.coins.large", "environment": "Sandbox"},
    )
    verifier = AppleStoreKitVerifier(
        bundle_id="com.musicfy.app", verify_signature=True, test_root_certs_pem=[root_pem]
    )
    tx = verifier.decode_transaction(token)
    assert tx["environment"] == StoreKitEnvironment.xcode.value
    assert tx["transaction_id"] == "7"
