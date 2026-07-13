"""ADR-013 D3 — fail-fast: prod не поднимается с выключенной проверкой подписи StoreKit.

`APPLE_STOREKIT_VERIFY_SIGNATURE=false` означает приём неподписанных JWS. Легально только для
APP_ENV ∈ {dev, test}; при APP_ENV=prod конфигурация обязана падать на старте (прецедент ADR-001).
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings

_DB = "postgresql+asyncpg://musicfy:musicfy@localhost:5544/musicfy"


def test_prod_with_verify_disabled_fails_fast():
    """APP_ENV=prod + verify=false → ValidationError (приложение не поднимается)."""
    with pytest.raises(ValidationError) as exc:
        Settings(APP_ENV="prod", APPLE_STOREKIT_VERIFY_SIGNATURE=False, DATABASE_URL=_DB)
    assert "APPLE_STOREKIT_VERIFY_SIGNATURE" in str(exc.value)


@pytest.mark.parametrize("env", ["dev", "test"])
def test_nonprod_with_verify_disabled_is_allowed(env: str):
    """dev/test с verify=false — легально (синтетические токены)."""
    settings = Settings(
        APP_ENV=env, APPLE_STOREKIT_VERIFY_SIGNATURE=False, DATABASE_URL=_DB
    )
    assert settings.APPLE_STOREKIT_VERIFY_SIGNATURE is False


def test_prod_with_verify_enabled_is_ok():
    """Боевой инвариант: prod + verify=true поднимается штатно."""
    settings = Settings(
        APP_ENV="prod", APPLE_STOREKIT_VERIFY_SIGNATURE=True, DATABASE_URL=_DB
    )
    assert settings.APP_ENV == "prod"
    assert settings.APPLE_STOREKIT_VERIFY_SIGNATURE is True
