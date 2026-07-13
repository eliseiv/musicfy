"""Верификация подписанных payload'ов App Store (StoreKit 2 / Server API).

App Store Server Notifications V2 и StoreKit-транзакции приходят как JWS (ES256,
с x5c-цепочкой: [leaf, intermediate, root]).

Подпись проверяется всегда (`verify_signature=True`, production-инвариант — ADR-013 D3,
защищён fail-fast'ом в `Settings`):
  1. Извлекаем x5c из заголовка JWS.
  2. Проверяем цепочку: leaf←intermediate←root, валидность по датам.
  3. Выбираем trust anchor по корню цепочки:
     - Apple Root CA - G3 (закреплён) → payload подлинный, claim `environment`
       (`Production` / `Sandbox`) удостоверён и ему можно доверять;
     - корень ∈ пине `APPLE_STOREKIT_TEST_ROOT_CERTS` (Xcode StoreKit Test, по умолчанию
       список пуст) → `environment` принудительно `Xcode`, независимо от claim;
     - иначе → `WebhookPayloadInvalid(untrusted_root)`.
  4. Проверяем подпись JWS публичным ключом leaf-сертификата (ES256).

При `verify_signature=False` payload декодируется без проверки подписи (интеграционные тесты
с синтетическими токенами). Легально только при `APP_ENV ∈ {dev, test}`: `environment` берётся
из claim с дефолтом `Xcode`. В prod такой конфиг не поднимается (см. `Settings`).

`environment` — дискриминатор области дедупа покупки (`BillingService._dedup_key`), поэтому он
обязан быть свойством *верифицированного* payload'а, а не самоаттестацией клиента.
"""
from __future__ import annotations

import base64
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import jwt
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app.api.errors import WebhookPayloadInvalid
from app.domain.enums import StoreKitEnvironment
from app.domain.providers.billing.apple_certs import apple_root_ca_g3_der

logger = logging.getLogger(__name__)


class AppleStoreKitVerifier:
    def __init__(
        self,
        *,
        bundle_id: str = "",
        verify_signature: bool = True,
        test_root_certs_pem: Sequence[str] = (),
    ) -> None:
        self._bundle_id = bundle_id
        self._verify_signature = verify_signature
        self._root_der = apple_root_ca_g3_der()
        self._test_root_ders = _load_test_roots(test_root_certs_pem)

    # ------------------------------------------------------------------ public

    def decode_transaction(self, signed_transaction: str) -> dict[str, Any]:
        claims, forced_env = self._verify_and_decode(signed_transaction)
        environment = self._resolve_environment(claims, forced_env)
        return {
            "transaction_id": str(claims.get("transactionId") or ""),
            "original_transaction_id": (
                str(claims["originalTransactionId"])
                if claims.get("originalTransactionId") else None
            ),
            "product_id": str(claims.get("productId") or ""),
            "type": claims.get("type"),
            "environment": environment.value,
            "purchase_date_ms": claims.get("purchaseDate"),
            "expires_date_ms": claims.get("expiresDate"),
            "revocation_date_ms": claims.get("revocationDate"),
            "raw": claims,
        }

    def decode_notification(self, signed_payload: str) -> dict[str, Any]:
        claims, _ = self._verify_and_decode(signed_payload)
        data = claims.get("data") or {}
        signed_tx = data.get("signedTransactionInfo")
        signed_renewal = data.get("signedRenewalInfo")
        transaction = self.decode_transaction(signed_tx) if signed_tx else None
        renewal = self._verify_and_decode(signed_renewal)[0] if signed_renewal else None
        return {
            "notification_type": claims.get("notificationType"),
            "subtype": claims.get("subtype"),
            "transaction": transaction,
            "renewal": renewal,
            "raw": claims,
        }

    # ----------------------------------------------------------------- private

    def _resolve_environment(
        self, claims: dict[str, Any], forced: StoreKitEnvironment | None
    ) -> StoreKitEnvironment:
        """Окружение транзакции по trust anchor'у цепочки + claim (ADR-013 D1/D3)."""
        if forced is not None:
            # Корень — закреплённый StoreKit Test root: claim игнорируется.
            return forced

        raw = str(claims.get("environment") or "")
        if not self._verify_signature:
            # dev/test: подпись не проверялась, доверять claim'у «по-боевому» нельзя.
            # Дефолт — самая узкая (per-user) область дедупа.
            try:
                return StoreKitEnvironment(raw)
            except ValueError:
                return StoreKitEnvironment.xcode

        # Корень — Apple Root CA - G3: claim удостоверён подписью Apple. Apple никогда не
        # подписывает Xcode-транзакции, поэтому всё, кроме явного Sandbox, трактуется как
        # Production — строжайшая (глобальная) область дедупа. Так claim `environment:
        # "Xcode"` в подлинном payload'е не может ослабить дедуп.
        if raw == StoreKitEnvironment.sandbox.value:
            return StoreKitEnvironment.sandbox
        return StoreKitEnvironment.production

    def _verify_and_decode(self, signed: str) -> tuple[dict[str, Any], StoreKitEnvironment | None]:
        """Возвращает (claims, forced_environment). forced != None → корень из тест-пина."""
        if not signed or signed.count(".") != 2:
            raise WebhookPayloadInvalid(details={"reason": "not_jws"})
        if not self._verify_signature:
            try:
                claims = jwt.decode(signed, options={"verify_signature": False})
            except jwt.PyJWTError as exc:
                raise WebhookPayloadInvalid(details={"reason": "bad_jws"}) from exc
            return (claims if isinstance(claims, dict) else {}), None

        try:
            header = jwt.get_unverified_header(signed)
        except jwt.PyJWTError as exc:
            raise WebhookPayloadInvalid(details={"reason": "bad_header"}) from exc
        x5c = header.get("x5c")
        # Боевая цепочка Apple = [leaf, intermediate, root]; Xcode StoreKit local testing
        # подписывает ОДНИМ self-signed cert (x5c длины 1). Допускаем len>=1 — trust anchor
        # всё равно обязан точно совпасть с пином (Apple Root G3 / StoreKit Test root)
        # в `_verify_chain`, поэтому единичный cert не ослабляет боевую проверку (ADR-013).
        if not isinstance(x5c, list) or not x5c:
            raise WebhookPayloadInvalid(details={"reason": "no_x5c"})

        leaf, forced_env = self._verify_chain(x5c)
        leaf_pub_pem = leaf.public_key().public_bytes(
            Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
        )
        try:
            claims = jwt.decode(signed, leaf_pub_pem, algorithms=["ES256"])
        except jwt.PyJWTError as exc:
            raise WebhookPayloadInvalid(details={"reason": "signature_invalid"}) from exc
        if not isinstance(claims, dict):
            raise WebhookPayloadInvalid(details={"reason": "bad_claims"})
        return claims, forced_env

    def _verify_chain(
        self, x5c: list[str]
    ) -> tuple[x509.Certificate, StoreKitEnvironment | None]:
        """Проверяет цепочку x5c. Возвращает (leaf, forced_environment)."""
        try:
            certs = [x509.load_der_x509_certificate(base64.b64decode(c)) for c in x5c]
        except Exception as exc:
            raise WebhookPayloadInvalid(details={"reason": "bad_cert"}) from exc

        now = datetime.now(UTC)
        for cert in certs:
            if not (cert.not_valid_before_utc <= now <= cert.not_valid_after_utc):
                raise WebhookPayloadInvalid(details={"reason": "cert_expired"})

        # Trust anchor: Apple Root CA - G3 (боевой) либо закреплённый StoreKit Test root.
        root_der = certs[-1].public_bytes(Encoding.DER)
        if root_der == self._root_der:
            forced_env: StoreKitEnvironment | None = None
        elif root_der in self._test_root_ders:
            forced_env = StoreKitEnvironment.xcode
            logger.info("storekit: payload signed by pinned StoreKit Test root → Xcode")
        else:
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

        return certs[0], forced_env


def _load_test_roots(pem_certs: Sequence[str]) -> frozenset[bytes]:
    """PEM-пин StoreKit Test root-сертификатов → множество DER для сравнения корня цепочки."""
    ders: set[bytes] = set()
    for pem in pem_certs:
        try:
            cert = x509.load_pem_x509_certificate(pem.encode("utf-8"))
        except (ValueError, TypeError) as exc:
            raise ValueError(
                "APPLE_STOREKIT_TEST_ROOT_CERTS: не удалось разобрать PEM-сертификат"
            ) from exc
        ders.add(cert.public_bytes(Encoding.DER))
    return frozenset(ders)
