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
        dnc_scrub_provider="ftc_registry",
        dnc_san="test-san",
        dnc_area_codes="313,248",
        public_base_url="https://calls.example.com",
        consent_authorized_sellers="Example Agency",
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
    # Outbound contact ships disarmed.
    assert values["OUTBOUND_ENABLED"] == "false"


# ── Production hardening: DNC provider, webhook URL, thresholds, states ──────

from pydantic import ValidationError  # noqa: E402


def test_production_with_rate_table_reset_refuses_to_start():
    with pytest.raises(UnsafeProductionConfigError, match="RESET_RATE_TABLE_ON_BOOT"):
        assert_production_safety(make_settings(reset_rate_table_on_boot=True))


def test_production_with_local_csv_dnc_provider_refuses_to_start():
    with pytest.raises(UnsafeProductionConfigError, match="ftc_registry"):
        assert_production_safety(make_settings(dnc_scrub_provider="local_csv"))


@pytest.mark.parametrize("missing", ["dnc_san", "dnc_area_codes"])
def test_production_ftc_provider_without_subscription_refuses_to_start(missing):
    with pytest.raises(UnsafeProductionConfigError, match="DNC_SAN and DNC_AREA_CODES"):
        assert_production_safety(make_settings(**{missing: ""}))


@pytest.mark.parametrize(
    "url, why",
    [
        ("", "empty"),
        ("http://calls.example.com", "not https"),
        ("https://abc123.ngrok-free.app", "temporary tunnel"),
        ("https://abc123.ngrok.io", "temporary tunnel"),
        ("https://quiet-river.trycloudflare.com", "temporary tunnel"),
        ("https://localhost:8443", "local address"),
    ],
)
def test_production_refuses_an_unusable_public_base_url(url, why):
    with pytest.raises(UnsafeProductionConfigError, match=why):
        assert_production_safety(make_settings(public_base_url=url))


def test_development_may_use_a_temporary_tunnel():
    assert_production_safety(make_settings(app_env="development", public_base_url="https://x.ngrok-free.app"))


@pytest.mark.parametrize("hot, warm", [(5, 5), (74, 50), (75, 49)])
def test_production_refuses_thresholds_below_documented_launch_values(hot, warm):
    with pytest.raises(UnsafeProductionConfigError, match="thresholds"):
        assert_production_safety(make_settings(score_hot_threshold=hot, score_warm_threshold=warm))


def test_production_accepts_stricter_thresholds():
    assert_production_safety(make_settings(score_hot_threshold=85, score_warm_threshold=60))


def test_production_with_no_active_states_refuses_to_start():
    with pytest.raises(UnsafeProductionConfigError, match="ACTIVE_STATES"):
        assert_production_safety(make_settings(active_states=" , "))


@pytest.mark.parametrize("states", ["MIC", "MI,ZZ", "PR"])
def test_unknown_campaign_states_are_rejected_in_every_environment(states):
    with pytest.raises(ValidationError, match="unsupported state codes"):
        make_settings(app_env="development", active_states=states)


def test_campaign_states_are_case_and_space_tolerant():
    s = make_settings(active_states=" mi, tx ,FL")
    assert s.active_states_list() == ["MI", "TX", "FL"]


def test_warm_threshold_above_hot_is_rejected():
    with pytest.raises(ValidationError, match="SCORE_WARM_THRESHOLD"):
        make_settings(app_env="development", score_hot_threshold=50, score_warm_threshold=75)


def test_production_with_zero_concurrency_refuses_to_start():
    with pytest.raises(UnsafeProductionConfigError, match="MAX_CONCURRENT_CALLS"):
        assert_production_safety(make_settings(max_concurrent_calls=0))
