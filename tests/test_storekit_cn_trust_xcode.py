"""ADR-014 — CN-trust Xcode StoreKit Test сертификатов за флагом `trust_xcode_test_certs`.

Платёжный путь + осознанно ослабляемая на проде проверка → покрытие критично.

Инварианты:
  * флаг ON + verify_signature=true: ЛЮБОЙ self-signed EC-cert с CN="StoreKit Testing in Xcode"
    (subject==issuer, валидная собственная подпись) → environment ПРИНУДИТЕЛЬНО `Xcode`, БЕЗ
    DER-пина — регресс на прод-кейс тестеров, у каждого свой уникальный DER;
  * флаг OFF: тот же cert → `untrusted_root` (прод по умолчанию строгий, поведение ADR-013);
  * подделка признаков (порченая self-подпись / чужой CN / не self-signed / RSA) → `untrusted_root`
    даже при флаге ON — §D3 доказывает владение ключом криптографически, а не по subject-строке;
  * боевой Apple-путь (root == trust anchor) и DER-пин НЕ затронуты новым флагом;
  * fail-fast `prod + verify_signature=false` не зависит от нового флага (D1).

Формат провайдера (SHARED v2): реальный Xcode StoreKit Test JWS — x5c ДЛИНЫ 1, единственный
self-signed EC-cert `CN="StoreKit Testing in Xcode"` (subject==issuer, серийник 1). Подтверждено
реальными образцами (Максим 2026-05-05, второй тестер 2026-04-09). Все фикстуры строят именно
этот формат in-memory через `cryptography`; реальный (истёкший) JWS Максима как фикстура НЕ
используется. Сроки cert'ов заведомо валидны, иначе сработал бы `cert_expired`.
"""
from __future__ import annotations

import base64
import datetime
import uuid

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from cryptography.x509.oid import NameOID
from pydantic import ValidationError

from app.api.errors import WebhookPayloadInvalid
from app.config import Settings
from app.domain.enums import StoreKitEnvironment
from app.domain.providers.billing.apple import AppleStoreKitVerifier
from app.domain.services.billing_service import BillingService

_XCODE_CN = "StoreKit Testing in Xcode"  # ровно константа Xcode (ADR-014 §D3)
_BUNDLE = "com.musicfy.app"


# --------------------------------------------------------------------------- helpers


def _build_cert(*, subject_cn, issuer_cn, public_key, signing_key, hash_alg=None, serial=1):
    """Собирает X.509 cert с явными subject/issuer и раздельными embedded-pubkey / signing-key.

    Раздельность нужна для NEGATIVE-кейса: self-signed по имени (subject==issuer), но подписан
    ДРУГИМ ключом, чем встроенный публичный → собственная подпись невалидна.
    """
    now = datetime.datetime.now(datetime.UTC)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject_cn)])
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer_cn)])
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(public_key)
        .serial_number(serial)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .sign(signing_key, hash_alg or hashes.SHA256())
    )


def _key_pem(key) -> bytes:
    return key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())


def _x5c(*certs) -> list[str]:
    return [base64.b64encode(c.public_bytes(Encoding.DER)).decode() for c in certs]


def _authentic_xcode_cert():
    """Реальный формат Xcode: единственный self-signed EC-cert CN=_XCODE_CN, серийник 1."""
    key = ec.generate_private_key(ec.SECP256R1())
    cert = _build_cert(subject_cn=_XCODE_CN, issuer_cn=_XCODE_CN, public_key=key.public_key(),
                       signing_key=key, serial=1)
    return cert, key


def _token(key_pem: bytes, x5c: list[str], claims: dict, *, alg="ES256") -> str:
    return jwt.encode(claims, key_pem, algorithm=alg, headers={"x5c": x5c})


def _tx_claims(**over) -> dict:
    base = {
        "transactionId": "42",
        "productId": "com.musicfy.coins.small",
        "purchaseDate": 1_700_000_000_000,
        "environment": "Production",  # claim лжёт — при Xcode-ветке ДОЛЖЕН игнорироваться
    }
    base.update(over)
    return base


# ============================================================ 1. happy path (флаг ON, не пиненный)


def test_flag_on_unpinned_self_signed_cn_cert_forces_xcode():
    """Флаг ON, валидный self-signed CN-cert, x5c len 1, НЕ в пине → claims + forced Xcode.

    Прямой регресс на прод-кейс тестеров: cert не запинен по DER, но CN-trust его принимает.
    """
    cert, key = _authentic_xcode_cert()
    x5c = _x5c(cert)
    assert len(x5c) == 1  # ровно формат Xcode, не искусственная многосертификатная цепочка
    token = _token(_key_pem(key), x5c, _tx_claims(transactionId="777"))
    verifier = AppleStoreKitVerifier(
        bundle_id=_BUNDLE, verify_signature=True, trust_xcode_test_certs=True
    )
    tx = verifier.decode_transaction(token)
    assert tx["environment"] == StoreKitEnvironment.xcode.value  # claim "Production" перебит
    assert tx["transaction_id"] == "777"
    assert tx["product_id"] == "com.musicfy.coins.small"


def test_flag_on_low_level_verify_and_decode_returns_forced_xcode():
    """Нижнеуровневый _verify_and_decode: возвращает claims + forced=Xcode (не None)."""
    cert, key = _authentic_xcode_cert()
    token = _token(_key_pem(key), _x5c(cert), _tx_claims(transactionId="9"))
    verifier = AppleStoreKitVerifier(
        bundle_id=_BUNDLE, verify_signature=True, trust_xcode_test_certs=True
    )
    claims, forced = verifier._verify_and_decode(token)
    assert claims["transactionId"] == "9"
    assert forced == StoreKitEnvironment.xcode


# ============================================================ 2. флаг OFF → untrusted_root


def test_flag_off_same_cert_is_untrusted_root():
    """Флаг OFF, тот же валидный self-signed CN-cert → untrusted_root (прод строгий by default)."""
    cert, key = _authentic_xcode_cert()
    token = _token(_key_pem(key), _x5c(cert), _tx_claims())
    verifier = AppleStoreKitVerifier(
        bundle_id=_BUNDLE, verify_signature=True, trust_xcode_test_certs=False
    )
    with pytest.raises(WebhookPayloadInvalid) as exc:
        verifier.decode_transaction(token)
    assert exc.value.details["reason"] == "untrusted_root"


# ============================================================ 3. NEGATIVE: порченая self-подпись


def test_flag_on_cn_match_but_broken_self_signature_is_untrusted_root():
    """Флаг ON: subject==issuer + CN-match + EC, но собственная подпись НЕВАЛИДНА → untrusted_root.

    Атака §D3: злоумышленник выставляет subject==issuer и нужный CN, но НЕ владеет ключом,
    соответствующим встроенному публичному (cert подписан другим ключом). Крипто-проверка
    self-подписи (`pub.verify(cert.signature, cert.tbs, ...)`) обязана это поймать: subject-строки
    недостаточно.
    """
    embedded_key = ec.generate_private_key(ec.SECP256R1())  # его pubkey кладём в cert
    wrong_signer = ec.generate_private_key(ec.SECP256R1())  # им подписываем → подпись «чужая»
    cert = _build_cert(
        subject_cn=_XCODE_CN, issuer_cn=_XCODE_CN,
        public_key=embedded_key.public_key(), signing_key=wrong_signer, serial=1,
    )
    # sanity: имя self-signed, ключ EC — все «дешёвые» признаки совпадают, ловит только крипто-шаг
    assert cert.subject == cert.issuer
    token = _token(_key_pem(embedded_key), _x5c(cert), _tx_claims())
    verifier = AppleStoreKitVerifier(
        bundle_id=_BUNDLE, verify_signature=True, trust_xcode_test_certs=True
    )
    with pytest.raises(WebhookPayloadInvalid) as exc:
        verifier.decode_transaction(token)
    assert exc.value.details["reason"] == "untrusted_root"


# ============================================================ 4. rogue: другой CN


def test_flag_on_rogue_self_signed_wrong_cn_is_untrusted_root():
    """Флаг ON: валидный self-signed EC-cert, но CN != _XCODE_CN → untrusted_root."""
    key = ec.generate_private_key(ec.SECP256R1())
    cert = _build_cert(
        subject_cn="Totally Legit Root", issuer_cn="Totally Legit Root",
        public_key=key.public_key(), signing_key=key,
    )
    token = _token(_key_pem(key), _x5c(cert), _tx_claims())
    verifier = AppleStoreKitVerifier(
        bundle_id=_BUNDLE, verify_signature=True, trust_xcode_test_certs=True
    )
    with pytest.raises(WebhookPayloadInvalid) as exc:
        verifier.decode_transaction(token)
    assert exc.value.details["reason"] == "untrusted_root"


# ============================================================ 5. CN-match, но НЕ self-signed


def test_flag_on_cn_match_but_not_self_signed_is_untrusted_root():
    """Флаг ON: CN-match, но subject != issuer (cert выпущен внешним CA) → untrusted_root."""
    ca_key = ec.generate_private_key(ec.SECP256R1())  # «CA», в x5c не кладём
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    cert = _build_cert(
        subject_cn=_XCODE_CN, issuer_cn="Some Issuing CA",  # subject != issuer
        public_key=leaf_key.public_key(), signing_key=ca_key,
    )
    assert cert.subject != cert.issuer
    token = _token(_key_pem(leaf_key), _x5c(cert), _tx_claims())
    verifier = AppleStoreKitVerifier(
        bundle_id=_BUNDLE, verify_signature=True, trust_xcode_test_certs=True
    )
    with pytest.raises(WebhookPayloadInvalid) as exc:
        verifier.decode_transaction(token)
    assert exc.value.details["reason"] == "untrusted_root"


# ============================================================ 6. CN-match, self-signed, но RSA-ключ


def test_flag_on_cn_match_self_signed_but_rsa_key_is_untrusted_root():
    """Флаг ON: self-signed CN-match, но публичный ключ RSA (не EC) → untrusted_root.

    Xcode/Apple leaf'ы всегда EC (ES256); RSA-ключ не проходит isinstance-проверку §D3.
    JWS подписан RS256 — до jwt.decode дело не доходит (untrusted_root бросается в резолве корня).
    """
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = _build_cert(
        subject_cn=_XCODE_CN, issuer_cn=_XCODE_CN,
        public_key=rsa_key.public_key(), signing_key=rsa_key,
    )
    assert cert.subject == cert.issuer  # self-signed, валидная RSA-подпись — ловит только EC-шаг
    token = _token(_key_pem(rsa_key), _x5c(cert), _tx_claims(), alg="RS256")
    verifier = AppleStoreKitVerifier(
        bundle_id=_BUNDLE, verify_signature=True, trust_xcode_test_certs=True
    )
    with pytest.raises(WebhookPayloadInvalid) as exc:
        verifier.decode_transaction(token)
    assert exc.value.details["reason"] == "untrusted_root"


# ==================================================== 7. не сломано: DER-пин при флаге OFF


def test_der_pin_still_forces_xcode_without_flag():
    """DER-пин (test_root_certs) при флаге OFF → forced Xcode. Путь пина не требует флага."""
    cert, key = _authentic_xcode_cert()
    cert_pem = cert.public_bytes(Encoding.PEM).decode()
    token = _token(_key_pem(key), _x5c(cert), _tx_claims(transactionId="55"))
    verifier = AppleStoreKitVerifier(
        bundle_id=_BUNDLE, verify_signature=True,
        test_root_certs_pem=[cert_pem], trust_xcode_test_certs=False,
    )
    tx = verifier.decode_transaction(token)
    assert tx["environment"] == StoreKitEnvironment.xcode.value
    assert tx["transaction_id"] == "55"


# ============================================= 8. не сломано: боевой Apple-путь ≥2 cert


@pytest.mark.parametrize("flag", [True, False])
@pytest.mark.parametrize(
    "claim_env,expected", [("Production", "Production"), ("Sandbox", "Sandbox")]
)
def test_apple_root_chain_forced_none_claim_honored_under_both_flags(flag, claim_env, expected):
    """Боевой путь: 2-cert цепочка leaf←root, root == trust anchor → forced=None, claim удостоверён.

    Apple Root CA - G3 форсировать нельзя (нет приватного ключа Apple), поэтому подставляем
    синтетический корень в `verifier._root_der` — так тестируется САМА ветка «root совпал с
    закреплённым anchor'ом»: forced_env=None и environment берётся из claim (Production/Sandbox),
    а CN-trust НЕ перехватывает (branch 1 раньше branch 3). Проверяется при обоих состояниях флага.
    """
    root_key = ec.generate_private_key(ec.SECP256R1())
    root = _build_cert(
        subject_cn="Apple Root CA - G3", issuer_cn="Apple Root CA - G3",
        public_key=root_key.public_key(), signing_key=root_key,
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = _build_cert(
        subject_cn="Apple Leaf", issuer_cn="Apple Root CA - G3",
        public_key=leaf_key.public_key(), signing_key=root_key,  # root подписывает leaf
    )
    x5c = _x5c(leaf, root)
    assert len(x5c) >= 2
    token = _token(_key_pem(leaf_key), x5c, _tx_claims(transactionId="7", environment=claim_env))
    verifier = AppleStoreKitVerifier(
        bundle_id=_BUNDLE, verify_signature=True, trust_xcode_test_certs=flag
    )
    verifier._root_der = root.public_bytes(Encoding.DER)  # подставляем anchor вместо Apple G3
    tx = verifier.decode_transaction(token)
    assert tx["environment"] == expected  # forced=None → claim удостоверён, НЕ Xcode
    assert tx["transaction_id"] == "7"


# ============================================================ 9. не сломано: fail-fast prod


def test_fail_fast_prod_verify_false_independent_of_new_flag():
    """prod + verify_signature=false → ValidationError, независимо от значения нового флага (D1)."""
    for flag in (True, False):
        with pytest.raises(ValidationError):
            Settings(
                APP_ENV="prod",
                APPLE_STOREKIT_VERIFY_SIGNATURE=False,
                APPLE_STOREKIT_TRUST_XCODE_TEST_CERTS=flag,
            )


def test_new_flag_on_is_legal_in_prod_when_verify_true():
    """D1: флаг ON при verify_signature=true ЛЕГАЛЕН в prod (fail-fast на него НЕ вешается)."""
    s = Settings(
        APP_ENV="prod",
        APPLE_STOREKIT_VERIFY_SIGNATURE=True,
        APPLE_STOREKIT_TRUST_XCODE_TEST_CERTS=True,
    )
    assert s.APPLE_STOREKIT_TRUST_XCODE_TEST_CERTS is True


# ============================================================ 10. дедуп-namespace CN-trust ветки


def test_cn_trust_dedup_key_is_per_user_xcode_namespace():
    """CN-trust cert → decoded env=Xcode → дедуп-ключ `Xcode:{user}:{tx}:{purchase_date_ms}`.

    Сквозной тест: доверие по CN даёт ровно тот же forced=Xcode, что и DER-пин, поэтому покупка
    попадает в per-user namespace (ADR-014 §D4), а не в глобальный Production/Sandbox.
    """
    cert, key = _authentic_xcode_cert()
    purchase_ms = 1_700_000_000_123
    token = _token(
        _key_pem(key), _x5c(cert),
        _tx_claims(transactionId="777", purchaseDate=purchase_ms, environment="Production"),
    )
    verifier = AppleStoreKitVerifier(
        bundle_id=_BUNDLE, verify_signature=True, trust_xcode_test_certs=True
    )
    tx = verifier.decode_transaction(token)
    assert tx["environment"] == StoreKitEnvironment.xcode.value

    user_id = uuid.uuid4()
    dedup_key = BillingService._dedup_key(user_id, tx)
    assert dedup_key == f"Xcode:{user_id}:777:{purchase_ms}"
    # per-user: другой пользователь с тем же чеком → другой ключ (не может тронуть чужой баланс)
    other_key = BillingService._dedup_key(uuid.uuid4(), tx)
    assert other_key != dedup_key
