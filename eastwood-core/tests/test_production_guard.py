"""
The production boot guard.

BYPASS_TCPA short-circuits the 8am-9pm local-time calling window in
domain.tcpa.in_tcpa_window. Dialing outside that window is a per-call TCPA
violation, so eastwood-core must refuse to start in production with it set.
The same guard refuses production with the DNC test allowlist populated, with
Twilio webhook signature validation disabled, or without an operational API
key — every one of those is a development affordance that must never carry a
live campaign.

Settings are always constructed with explicit values here so ambient
environment variables (a developer's .env, the compose container's env) can
never change what these tests prove.
"""
from __future__ import annotations

import pytest

from config.settings import Settings, UnsafeProductionConfigError, assert_production_safety


def make_settings(**overrides) -> Settings:
    """A production-safe baseline; tests override one field at a time."""
    kwargs: dict = dict(
        _env_file=None,
        app_env="production",
        bypass_tcpa=False,
        dnc_test_allowlist="",
        twilio_signature_validation_enabled=True,
        eastwood_api_key="a-production-api-key",
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


def test_production_with_tcpa_bypass_refuses_to_start():
    with pytest.raises(UnsafeProductionConfigError, match="BYPASS_TCPA"):
        assert_production_safety(make_settings(bypass_tcpa=True))


def test_production_without_tcpa_bypass_starts():
    assert_production_safety(make_settings())


def test_development_with_tcpa_bypass_starts():
    """The escape hatch stays usable off production — that is its purpose."""
    assert_production_safety(make_settings(app_env="development", bypass_tcpa=True))


def test_production_with_dnc_test_allowlist_refuses_to_start():
    with pytest.raises(UnsafeProductionConfigError, match="DNC_TEST_ALLOWLIST"):
        assert_production_safety(make_settings(dnc_test_allowlist="+15555550100"))


def test_production_with_signature_validation_disabled_refuses_to_start():
    with pytest.raises(UnsafeProductionConfigError, match="TWILIO_SIGNATURE_VALIDATION"):
        assert_production_safety(make_settings(twilio_signature_validation_enabled=False))


def test_development_may_disable_signature_validation():
    """Disabling validation is an explicit, development-only configuration."""
    assert_production_safety(
        make_settings(app_env="development", twilio_signature_validation_enabled=False)
    )


def test_production_without_api_key_refuses_to_start():
    with pytest.raises(UnsafeProductionConfigError, match="EASTWOOD_API_KEY"):
        assert_production_safety(make_settings(eastwood_api_key=""))


def test_development_without_api_key_starts():
    """Dev boots without a key — /api routes then fail closed per request."""
    assert_production_safety(make_settings(app_env="development", eastwood_api_key=""))


@pytest.mark.parametrize("app_env", ["PRODUCTION", "Production", " production "])
def test_production_detection_is_not_case_or_whitespace_sensitive(app_env):
    with pytest.raises(UnsafeProductionConfigError):
        assert_production_safety(make_settings(app_env=app_env, bypass_tcpa=True))


def test_example_env_ships_safe_defaults():
    """.env.example is the template every deployment starts from."""
    import pathlib

    example = pathlib.Path(__file__).resolve().parents[2] / ".env.example"
    assert example.exists(), ".env.example must exist as the template for .env"

    values: dict[str, str] = {}
    for line in example.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()

    assert values["APP_ENV"] == "development"
    assert values["BYPASS_TCPA"] == "false"
    assert values["RESET_RATE_TABLE_ON_BOOT"] == "false"
    assert values["SCORE_HOT_THRESHOLD"] == "75"
    assert values["SCORE_WARM_THRESHOLD"] == "50"
    assert values["TWILIO_SIGNATURE_VALIDATION_ENABLED"] == "true"
    # The key ships as an empty placeholder — the real value stays local.
    assert values["EASTWOOD_API_KEY"] == ""
