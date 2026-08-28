"""Юнит-тесты дефенсивного парсера Adapty (ADR-019).

Парсер обязан не бросать ни на одном кривом входе: любое исключение здесь превратилось бы в
500 и в вечный ретрай со стороны Adapty.
"""
from __future__ import annotations

import datetime
import uuid

import pytest

from app.domain.providers.billing import adapty as parser
from app.domain.providers.billing.adapty import ParsedAdaptyEvent


def _make(**kwargs) -> ParsedAdaptyEvent:
    defaults = dict(
        event_id="e1",
        event_type="subscription_started",
        customer_user_id=uuid.uuid4(),
        vendor_product_id="week_6.99_not_trial",
        expires_at=None,
        transaction_id=None,
        original_transaction_id=None,
        is_active=None,
        access_level_id=None,
        will_renew=None,
    )
    defaults.update(kwargs)
    return ParsedAdaptyEvent(**defaults)  # type: ignore[arg-type]


# --- event_id --------------------------------------------------------------


def test_event_id_prefers_profile_event_id():
    body = {"profile_event_id": "real", "event_id": "legacy"}
    assert parser.parse_event_id(body) == "real"


def test_event_id_reads_from_event_properties():
    assert parser.parse_event_id({"event_properties": {"profile_event_id": "x"}}) == "x"


def test_event_id_coerces_bare_int():
    """Adapty присылает id-поля голым числом."""
    assert parser.parse_event_id({"profile_event_id": 410003298316682}) == "410003298316682"


def test_event_id_rejects_bool():
    """isinstance(True, int) — True; `True` не должен стать строкой 'True'."""
    assert parser.parse_event_id({"profile_event_id": True}) is None


def test_event_id_missing_is_none():
    assert parser.parse_event_id({}) is None


# --- прочие поля -----------------------------------------------------------


def test_event_type_is_lowercased():
    assert parser.parse_event_type({"event_type": "Subscription_Started"}) == (
        "subscription_started"
    )


def test_event_type_missing_is_empty_string():
    assert parser.parse_event_type({}) == ""


def test_customer_user_id_non_uuid_is_none():
    assert parser.parse_customer_user_id({"customer_user_id": "nope"}) is None


def test_customer_user_id_from_profile_block():
    u = uuid.uuid4()
    assert parser.parse_customer_user_id({"profile": {"customer_user_id": str(u)}}) == u


def test_profile_not_a_dict_does_not_raise():
    assert parser.parse_customer_user_id({"profile": "surprise"}) is None


def test_vendor_product_id_prefers_event_properties():
    body = {"event_properties": {"vendor_product_id": "ep"}, "vendor_product_id": "flat"}
    assert parser.parse_vendor_product_id(body) == "ep"


def test_transaction_id_numeric_from_event_properties():
    body = {"event_properties": {"transaction_id": 410003298316682}}
    assert parser.parse_transaction_id(body) == "410003298316682"


@pytest.mark.parametrize(
    "raw,expected_year",
    [("2026-07-07T09:05:46Z", 2026), ("2026-07-07T09:05:46+00:00", 2026)],
)
def test_expires_at_parses_z_and_offset(raw, expected_year):
    parsed = parser.parse_expires_at({"event_properties": {"subscription_expires_at": raw}})
    assert parsed is not None
    assert parsed.year == expected_year
    assert parsed.tzinfo is not None


def test_expires_at_naive_becomes_utc():
    parsed = parser.parse_expires_at({"expires_at": "2026-07-07T09:05:46"})
    assert parsed is not None and parsed.tzinfo == datetime.UTC


def test_expires_at_unparseable_is_none_not_raise():
    assert parser.parse_expires_at({"expires_at": "вчера"}) is None


@pytest.mark.parametrize("value", [1, 0, "true", "false", None])
def test_is_active_non_bool_is_none(value):
    """Строгость намеренная: от is_active зависит выдача/отзыв доступа."""
    assert parser.parse_is_active({"event_properties": {"is_active": value}}) is None


@pytest.mark.parametrize("value", [True, False])
def test_is_active_real_bool_passes(value):
    assert parser.parse_is_active({"event_properties": {"is_active": value}}) is value


# --- classify_event --------------------------------------------------------


@pytest.mark.parametrize(
    "event_type", ["trial_started", "subscription_started", "subscription_renewed"]
)
def test_granting_events(event_type):
    assert parser.classify_event(_make(event_type=event_type)) == parser.SEM_GRANTING


@pytest.mark.parametrize("event_type", ["subscription_expired", "subscription_cancelled"])
def test_expiring_events(event_type):
    assert parser.classify_event(_make(event_type=event_type)) == parser.SEM_EXPIRING


@pytest.mark.parametrize(
    "event_type", ["subscription_renewal_cancelled", "trial_renewal_cancelled"]
)
def test_renewal_cancellation_is_noop(event_type):
    """Отмена автопродления — не отзыв доступа."""
    assert parser.classify_event(_make(event_type=event_type)) == parser.SEM_NOOP


def test_access_level_updated_premium_active_grants():
    event = _make(
        event_type="access_level_updated", is_active=True, access_level_id="premium"
    )
    assert parser.classify_event(event) == parser.SEM_GRANTING


def test_access_level_updated_inactive_expires():
    event = _make(event_type="access_level_updated", is_active=False)
    assert parser.classify_event(event) == parser.SEM_EXPIRING


def test_access_level_updated_active_non_premium_is_noop():
    event = _make(
        event_type="access_level_updated", is_active=True, access_level_id="basic"
    )
    assert parser.classify_event(event) == parser.SEM_NOOP


def test_access_level_updated_unknown_is_active_does_not_revoke():
    """is_active не пришёл → доступ НЕ отзываем."""
    event = _make(event_type="access_level_updated", is_active=None)
    assert parser.classify_event(event) == parser.SEM_NOOP


def test_known_events_composition():
    assert parser.KNOWN_EVENTS == (
        parser.GRANTING_EVENTS
        | parser.EXPIRING_EVENTS
        | parser.NOOP_EVENTS
        | parser.CONDITIONAL_EVENTS
    )
    assert "non_subscription_purchase" not in parser.KNOWN_EVENTS
