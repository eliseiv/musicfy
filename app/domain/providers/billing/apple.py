"""Верификация подписанных payload'ов App Store (StoreKit 2 / Server API).

App Store Server Notifications V2 и StoreKit-транзакции приходят как JWS (ES256,
с x5c-цепочкой Apple: [leaf, intermediate, Apple Root CA - G3]).

При `verify_signature=True` (production-дефолт):
  1. Извлекаем x5c из заголовка JWS.
  2. Проверяем цепочку: leaf←intermediate←root, валидность по датам.
  3. Корень обязан совпадать с закреплённым Apple Root CA - G3.
  4. Проверяем подпись JWS публичным ключом leaf-сертификата (ES256).

При `verify_signature=False` (dev/test/sandbox) payload декодируется без проверки
подписи (используется в интеграционных тестах с синтетическими токенами).
"""
from __future__ import annotations

import base64
import logging
from datetime import UTC, datetime
from typing import Any

import jwt
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app.api.errors import WebhookPayloadInvalid
from app.domain.providers.billing.apple_certs import apple_root_ca_g3_der

logger = logging.getLogger(__name__)


class AppleStoreKitVerifier:
    def __init__(self, *, bundle_id: str = "", verify_signature: bool = True) -> None:
        self._bundle_id = bundle_id
        self._verify_signature = verify_signature
        self._root_der = apple_root_ca_g3_der()

    # ------------------------------------------------------------------ public

    def decode_transaction(self, signed_transaction: str) -> dict[str, Any]:
        claims = self._verify_and_decode(signed_transaction)
        return {
            "transaction_id": str(claims.get("transactionId") or ""),
            "original_transaction_id": (
                str(claims["originalTransactionId"])
                if claims.get("originalTransactionId") else None
            ),
            "product_id": str(claims.get("productId") or ""),
            "type": claims.get("type"),
            "expires_date_ms": claims.get("expiresDate"),
            "revocation_date_ms": claims.get("revocationDate"),
            "raw": claims,
        }

    def decode_notification(self, signed_payload: str) -> dict[str, Any]:
        claims = self._verify_and_decode(signed_payload)
        data = claims.get("data") or {}
        signed_tx = data.get("signedTransactionInfo")
        signed_renewal = data.get("signedRenewalInfo")
        transaction = self.decode_transaction(signed_tx) if signed_tx else None
        renewal = self._verify_and_decode(signed_renewal) if signed_renewal else None
        return {
            "notification_type": claims.get("notificationType"),
            "subtype": claims.get("subtype"),
            "transaction": transaction,
            "renewal": renewal,
            "raw": claims,
        }

    # ----------------------------------------------------------------- private

    def _verify_and_decode(self, signed: str) -> dict[str, Any]:
        if not signed or signed.count(".") != 2:
            raise WebhookPayloadInvalid(details={"reason": "not_jws"})
        if not self._verify_signature:
            try:
                claims = jwt.decode(signed, options={"verify_signature": False})
            except jwt.PyJWTError as exc:
                raise WebhookPayloadInvalid(details={"reason": "bad_jws"}) from exc
            return claims if isinstance(claims, dict) else {}

        try:
            header = jwt.get_unverified_header(signed)
        except jwt.PyJWTError as exc:
            raise WebhookPayloadInvalid(details={"reason": "bad_header"}) from exc
        x5c = header.get("x5c")
        if not isinstance(x5c, list) or len(x5c) < 2:
            raise WebhookPayloadInvalid(details={"reason": "no_x5c"})

        leaf = self._verify_chain(x5c)
        leaf_pub_pem = leaf.public_key().public_bytes(
            Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
        )
        try:
            claims = jwt.decode(signed, leaf_pub_pem, algorithms=["ES256"])
        except jwt.PyJWTError as exc:
            raise WebhookPayloadInvalid(details={"reason": "signature_invalid"}) from exc
        if not isinstance(claims, dict):
            raise WebhookPayloadInvalid(details={"reason": "bad_claims"})
        return claims

    def _verify_chain(self, x5c: list[str]) -> x509.Certificate:
        """Проверяет цепочку x5c и возвращает leaf-сертификат."""
        try:
            certs = [x509.load_der_x509_certificate(base64.b64decode(c)) for c in x5c]
        except Exception as exc:
            raise WebhookPayloadInvalid(details={"reason": "bad_cert"}) from exc

        now = datetime.now(UTC)
        for cert in certs:
            if not (cert.not_valid_before_utc <= now <= cert.not_valid_after_utc):
                raise WebhookPayloadInvalid(details={"reason": "cert_expired"})

        # Корень цепочки обязан быть закреплённым Apple Root CA - G3.
        if certs[-1].public_bytes(Encoding.DER) != self._root_der:
            raise WebhookPayloadInvalid(details={"reason": "untrusted_root"})

        # Каждый сертификат подписан следующим (child ← parent).
        for child, parent in zip(certs, certs[1:], strict=False):
            try:
                parent.public_key().verify(
                    child.signature,
                    child.tbs_certificate_bytes,
                    ec.ECDSA(child.signature_hash_algorithm),
                )
            except (InvalidSignature, ValueError, TypeError) as exc:
                raise WebhookPayloadInvalid(details={"reason": "chain_invalid"}) from exc

        return certs[0]
