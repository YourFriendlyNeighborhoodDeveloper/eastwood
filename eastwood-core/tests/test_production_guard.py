"""
The production boot guard.

BYPASS_TCPA short-circuits the 8am-9pm local-time calling window in
domain.tcpa.in_tcpa_window. Dialing outside that window is a per-call TCPA
violation, so eastwood-core must refuse to start in production with it set.
"""
from __future__ import annotations

import pytest

from config.settings import Settings, UnsafeProductionConfigError, assert_production_safety


def test_production_with_tcpa_bypass_refuses_to_start():
    settings = Settings(app_env="production", bypass_tcpa=True)
    with pytest.raises(UnsafeProductionConfigError, match="BYPASS_TCPA"):
        assert_production_safety(settings)


def test_production_without_tcpa_bypass_starts():
    assert_production_safety(Settings(app_env="production", bypass_tcpa=False))


def test_development_with_tcpa_bypass_starts():
    """The escape hatch stays usable off production — that is its purpose."""
    assert_production_safety(Settings(app_env="development", bypass_tcpa=True))


@pytest.mark.parametrize("app_env", ["PRODUCTION", "Production", " production "])
def test_production_detection_is_not_case_or_whitespace_sensitive(app_env):
    with pytest.raises(UnsafeProductionConfigError):
        assert_production_safety(Settings(app_env=app_env, bypass_tcpa=True))


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
