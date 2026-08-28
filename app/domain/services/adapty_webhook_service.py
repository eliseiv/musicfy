"""AdaptyWebhookService: дефенсивный разбор → дедуп → подписка → монеты → лог (ADR-019).

`handle(raw)` НИКОГДА не бросает на кривом/незнакомом payload — он возвращает исход
`ignored` / `duplicate` / `applied`, и роутер отображает каждый из них в HTTP 200. Бросает он
только на реальном внутреннем сбое (например, недоступна БД): тогда транзакция откатывается,
роутер отдаёт 500, Adapty ретраит — и на ретрае `event_id` снова свободен (INSERT откатился),
поэтому переобработка чистая.

Почему 200 на мусор. Adapty ретраит любой не-2xx бесконечно, а при сохранении вебхука шлёт
проверочный пинг с пустым/не-JSON телом и не сохранит конфигурацию без 2xx. Pydantic-модель
запроса дала бы 422 и вечный ретрай, поэтому тело читается сырым.

Два независимых слоя идемпотентности:
  1. Дедуп ДОСТАВКИ — единственный оператор
     `INSERT INTO adapty_webhook_events ... ON CONFLICT (event_id) DO NOTHING RETURNING`.
     Пустой RETURNING → `duplicate` → ни одной мутации.
  2. Идемпотентность НАЧИСЛЕНИЯ — ledger-ключ `adapty-txn:{transaction_id}`.
Оба нужны: одна реальная покупка порождает НЕСКОЛЬКО событий Adapty с разными
`profile_event_id`, но одним `transaction_id`. Дедуп только по событию начислил бы монеты
трижды; дедуп только по транзакции не защитил бы от повторной доставки того же события.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.enums import (
    BillingProvider,
    CreditLedgerKind,
    CreditSource,
    SubscriptionStatus,
)
from app.domain.models.billing import Product
from app.domain.providers.billing import adapty as parser
from app.domain.providers.billing.adapty import ParsedAdaptyEvent
from app.domain.repositories.credits import CreditsRepository
from app.domain.repositories.products import ProductsRepository
from app.domain.services.webhook_user_resolve import resolve_webhook_user

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebhookOutcome:
    """Исход обработки одного вызова вебхука. Роутер заворачивает его в HTTP-200 JSON."""

    result: str  # "ignored" | "duplicate" | "applied"
    reason: str | None = None
    event_type: str | None = None


def _ignored(reason: str | None = None, event_type: str | None = None) -> WebhookOutcome:
    return WebhookOutcome(result="ignored", reason=reason, event_type=event_type)


def _level_for(result: str, reason: str | None) -> int:
    """Уровень лога по исходу.

    WARNING — класс инцидента «Adapty аутентифицировался и прислал событие, но монеты не
    доехали»: `user_not_found`, `missing_customer_user_id` и эхо неизвестного `event_type`
    (единственный `ignored` с `reason is None`). Именно на них вешаются алерты.
    Пустое тело — низкосигнальный проверочный пинг Adapty, DEBUG.
    """
    if result in ("applied", "duplicate"):
        return logging.INFO
    # result == "ignored"
    if reason in ("user_not_found", "missing_customer_user_id"):
        return logging.WARNING
    if reason is None:
        return logging.WARNING
    if reason == "empty_body":
        return logging.DEBUG
    # invalid_json | not_an_object | missing_event_id
    return logging.INFO


class AdaptyWebhookService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        fallback_coins_grant: int,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._fallback_coins_grant = fallback_coins_grant

    def _log_outcome(
        self,
        outcome: WebhookOutcome,
        *,
        event_type: str | None = None,
        event_id: str | None = None,
        customer_user_id: uuid.UUID | None = None,
        resolved_via: str | None = None,
        resolved_user_id: uuid.UUID | None = None,
    ) -> WebhookOutcome:
        """Пишет ровно одну структурную запись `adapty_webhook_outcome` и возвращает исход.

        Возвращает тот же объект, чтобы оборачивать существующие `return` без изменения
        потока управления. Логируется только фиксированный allowlist; сырое тело и bearer
        не логируются никогда. `customer_user_id` — ИСХОДНЫЙ идентификатор от Adapty
        (deviceId либо наш userId), `resolved_user_id` — уже наш внутренний UUID.
        Отсутствие поля в записи означает «не распарсено / не резолвнуто».
        """
        fields = {
            "result": outcome.result,
            "reason": outcome.reason,
            "event_type": event_type,
            "event_id": event_id,
            "customer_user_id": str(customer_user_id) if customer_user_id else None,
            "resolved_via": resolved_via,
            "resolved_user_id": str(resolved_user_id) if resolved_user_id else None,
        }
        present = {k: v for k, v in fields.items() if v is not None}
        logger.log(
            _level_for(outcome.result, outcome.reason),
            "adapty_webhook_outcome %s",
            " ".join(f"{k}={v}" for k, v in present.items()),
        )
        return outcome

    async def handle(self, raw: bytes) -> WebhookOutcome:
        """Обрабатывает одно сырое тело вебхука. Всегда даёт исход, отображаемый в 200.

        Предварительная валидация (пустое / не-JSON / не объект / нет id / нет пользователя /
        пользователь не найден / неизвестный тип) не пишет в БД вообще. Распознанное событие
        применяется в ОДНОЙ транзакции; реальный сбой БД пробрасывается → rollback → 500.
        """
        # --- Этап 1: форма тела (без БД) ---
        if not raw:
            return self._log_outcome(_ignored("empty_body"))
        try:
            body: Any = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return self._log_outcome(_ignored("invalid_json"))
        if not isinstance(body, dict):
            return self._log_outcome(_ignored("not_an_object"))

        # --- Этап 2: дефенсивный разбор полей (без БД) ---
        event_id = parser.parse_event_id(body)
        if event_id is None:
            return self._log_outcome(_ignored("missing_event_id"))
        # event_type парсится ДО проверки customer_user_id (обе операции чистые), чтобы
        # WARNING про отсутствующий идентификатор нёс тип события: оператор видит
        # «пришёл trial_started без customer_user_id», а не безликую причину.
        event_type = parser.parse_event_type(body)
        customer_user_id = parser.parse_customer_user_id(body)
        if customer_user_id is None:
            # Отсутствующий/не-UUID идентификатор эквивалентен «пользователь не найден», но
            # причина различается: пока iOS не вызвал `Adapty.identify`, в payload есть
            # только внутренний `profile_id` Adapty — это ожидаемая причина.
            return self._log_outcome(
                _ignored("missing_customer_user_id"),
                event_type=event_type or None,
                event_id=event_id,
            )

        async with self._sessionmaker() as session:
            async with session.begin():
                # --- Этап 3: двухступенчатый резолв пользователя (чтение; не создаём) ---
                resolved = await resolve_webhook_user(session, customer_user_id)
                if resolved is None:
                    return self._log_outcome(
                        _ignored("user_not_found"),
                        event_type=event_type,
                        event_id=event_id,
                        customer_user_id=customer_user_id,
                    )
                resolved_user_id, resolved_via = resolved

                # --- Этап 4: диспетчеризация по типу события ---
                if event_type not in parser.KNOWN_EVENTS:
                    # Эхо нормализованного типа, чтобы оператор видел, что именно пришло.
                    # Ни аудита, ни мутаций: неизвестный тип — «событие не произошло».
                    return self._log_outcome(
                        _ignored(event_type=event_type),
                        event_type=event_type,
                        event_id=event_id,
                        customer_user_id=customer_user_id,
                        resolved_via=resolved_via,
                        resolved_user_id=resolved_user_id,
                    )

                event = ParsedAdaptyEvent(
                    event_id=event_id,
                    event_type=event_type,
                    customer_user_id=customer_user_id,
                    vendor_product_id=parser.parse_vendor_product_id(body),
                    expires_at=parser.parse_expires_at(body),
                    transaction_id=parser.parse_transaction_id(body),
                    original_transaction_id=parser.parse_original_transaction_id(body),
                    is_active=parser.parse_is_active(body),
                    access_level_id=parser.parse_access_level_id(body),
                    will_renew=parser.parse_will_renew(body),
                )
                return await self._apply(
                    session, event, body, resolved_user_id, resolved_via
                )

    async def _apply(
        self,
        session: AsyncSession,
        event: ParsedAdaptyEvent,
        body: dict[str, Any],
        resolved_user_id: uuid.UUID,
        resolved_via: str,
    ) -> WebhookOutcome:
        """Применяет распознанное событие в открытой транзакции вызывающего.

        `INSERT ... ON CONFLICT DO NOTHING RETURNING` — единственная точка дедупа доставки:
        пустой RETURNING значит, что это событие уже записала предыдущая (или параллельная)
        доставка → `duplicate`, без мутаций. Дальше семантика резолвится `classify_event`:
        GRANTING обновляет подписку и начисляет монеты, EXPIRING помечает подписку
        завершённой (монеты не отзываем), NOOP не трогает ни доступ, ни монеты.

        Все записи адресуются `resolved_user_id` — нашему внутреннему userId, а НЕ
        `event.customer_user_id`, который может быть deviceId.
        """
        inserted = await session.scalar(
            text(
                "INSERT INTO adapty_webhook_events (event_id, user_id, event_type, payload) "
                "VALUES (:event_id, :uid, :event_type, CAST(:payload AS JSONB)) "
                "ON CONFLICT (event_id) DO NOTHING "
                "RETURNING event_id"
            ),
            {
                "event_id": event.event_id,
                "uid": str(resolved_user_id),
                "event_type": event.event_type,
                "payload": json.dumps(body),
            },
        )
        if inserted is None:
            return self._log_outcome(
                WebhookOutcome(result="duplicate"),
                event_type=event.event_type,
                event_id=event.event_id,
                customer_user_id=event.customer_user_id,
                resolved_via=resolved_via,
                resolved_user_id=resolved_user_id,
            )

        semantics = parser.classify_event(event)
        credits = CreditsRepository(session)

        if semantics == parser.SEM_GRANTING:
            product = await self._product_for(session, event.vendor_product_id)
            await self._activate_subscription(credits, event, resolved_user_id, product)
            # transaction_id уникален на период оплаты — это и есть верный ключ гранта.
            # original_transaction_id постоянен на всю цепочку подписки: сделав его
            # первичным, мы схлопнули бы продление с первичной покупкой, и пользователь не
            # получил бы монеты за новый период. event_id — крайний fallback.
            txn = (
                event.transaction_id
                or event.original_transaction_id
                or event.event_id
            )
            await self._grant_coins(credits, event, txn, resolved_user_id, product)
        elif semantics == parser.SEM_EXPIRING:
            await self._expire_subscription(credits, event, resolved_user_id)
        # SEM_NOOP: отмена автопродления — доступ сохраняется до конца периода. Ни статус,
        # ни expires_at, ни монеты не трогаем; событие остаётся в журнале для диагностики.

        return self._log_outcome(
            WebhookOutcome(result="applied"),
            event_type=event.event_type,
            event_id=event.event_id,
            customer_user_id=event.customer_user_id,
            resolved_via=resolved_via,
            resolved_user_id=resolved_user_id,
        )

    async def _product_for(
        self, session: AsyncSession, vendor_product_id: str | None
    ) -> Product | None:
        """Продукт каталога по `vendor_product_id` (побайтовое совпадение, как в StoreKit)."""
        if not vendor_product_id:
            return None
        return await ProductsRepository(session).get_by_external_id(vendor_product_id)

    def _coins_for(self, product: Product | None) -> int:
        """Сколько монет даёт продукт.

        Источник истины — каталог `products` (та же таблица, что читает StoreKit-путь), а НЕ
        payload вебхука и НЕ имя продукта: количество, выведенное из внешних данных, — это
        дыра в защите от подделки. Fallback применяется, только когда продукт ещё не засеян,
        чтобы платящий пользователь не остался без монет.
        """
        if product is None:
            return self._fallback_coins_grant
        coins = int((product.grants or {}).get("coins") or 0)
        return coins if coins > 0 else self._fallback_coins_grant

    async def _activate_subscription(
        self,
        credits: CreditsRepository,
        event: ParsedAdaptyEvent,
        user_id: uuid.UUID,
        product: Product | None,
    ) -> None:
        """GRANTING → подписка активна; провайдер помечается `adapty`."""
        await credits.upsert_subscription(
            user_id=user_id,
            values={
                "status": SubscriptionStatus.active,
                "provider": BillingProvider.adapty,
                "product_external_id": (
                    product.external_product_id if product else event.vendor_product_id
                ),
                "original_transaction_id": event.original_transaction_id,
                "expires_at": event.expires_at,
                "will_renew": event.will_renew,
            },
        )

    async def _expire_subscription(
        self,
        credits: CreditsRepository,
        event: ParsedAdaptyEvent,
        user_id: uuid.UUID,
    ) -> None:
        """EXPIRING → статус завершён. `product_external_id` / `expires_at` не трогаем.

        `subscription_expired` — истечение срока, `subscription_cancelled` — отмена; в
        существующей доменной модели это `expired` и `canceled` соответственно (те же
        значения, что пишет StoreKit-путь в `_handle_revocation`). Монеты не отзываются:
        начисленное за оплаченный период остаётся у пользователя.
        """
        status = (
            SubscriptionStatus.expired
            if event.event_type == "subscription_expired"
            else SubscriptionStatus.canceled
        )
        sub = await credits.get_subscription_for_update(user_id)
        if sub is None:
            await credits.upsert_subscription(
                user_id=user_id,
                values={
                    "status": status,
                    "provider": BillingProvider.adapty,
                    "will_renew": event.will_renew,
                },
            )
            return
        sub.status = status
        if event.will_renew is not None:
            sub.will_renew = event.will_renew

    async def _grant_coins(
        self,
        credits: CreditsRepository,
        event: ParsedAdaptyEvent,
        txn: str,
        user_id: uuid.UUID,
        product: Product | None,
    ) -> None:
        """Начисляет монеты, идемпотентно по `adapty-txn:{txn}`.

        Один оплаченный период = ровно один грант, сколько бы granting-событий Adapty по нему
        ни прислал; каждое продление (новый `transaction_id`) начисляет заново. Namespace
        ключа `adapty-` отделён от StoreKit-пути (`purchase:{dedup_key}`) — см. ADR-019 §Риски.
        """
        coins = self._coins_for(product)
        if coins <= 0:
            return
        newly = await credits.append_ledger(
            user_id=user_id,
            kind=CreditLedgerKind.credit_subscription_grant,
            amount=coins,
            source=CreditSource.subscription,
            reason="adapty_subscription",
            ref_type="adapty_transaction",
            ref_id=txn,
            idempotency_key=f"adapty-txn:{txn}",
        )
        if not newly:
            return
        wallet = await credits.ensure_wallet(user_id)
        wallet.coins_available += coins
