"""
Dev test-number allowlist for the DNC scrub gate.

A number the operator personally owns can scrub as `exempt` for end-to-end
testing without a provider — so we can dial our own phone even though the
default local_csv provider clears nobody. This is deliberately narrow: only the
exact numbers listed are exempt, every other number still fails closed, and the
allowlist is forbidden in production by the boot guard.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from config.settings import Settings, UnsafeProductionConfigError, assert_production_safety
from domain.compliance import (
    LocalCsvScrubProvider,
    ScrubResult,
    is_dialable,
    parse_test_allowlist,
    scrub_number,
)

NOW = datetime(2026, 7, 12, tzinfo=timezone.utc)
TEST_NUMBER = "+13135550199"
STRANGER = "+13135550000"


class _Settings:
    def __init__(self, allowlist: str = ""):
        self.dnc_test_allowlist = allowlist


def _provider() -> LocalCsvScrubProvider:
    # Blocklist-only provider with no file → clears nobody; every number is unknown.
    return LocalCsvScrubProvider("/does/not/exist.csv")


# ── (a) an allowlisted number returns exempt ─────────────────────────────────

async def test_allowlisted_number_scrubs_exempt_without_a_provider():
    class _ExplodingProvider:
        name = "boom"
        async def scrub(self, phone):
            raise AssertionError("provider must not be called for an allowlisted number")

    result = await scrub_number(
        TEST_NUMBER, provider=_ExplodingProvider(), settings=_Settings(TEST_NUMBER), now=NOW
    )

    assert result.status == "exempt"
    assert result.source == "test_allowlist"
    assert result.provider == "test_allowlist"
    assert is_dialable(result.status) is True
    assert result.audit["reason"] == "operator_test_allowlist"


async def test_allowlist_matches_only_exact_entries():
    settings = _Settings(f"{TEST_NUMBER}, +15550009999")
    assert (await scrub_number(TEST_NUMBER, provider=_provider(), settings=settings, now=NOW)).status == "exempt"
    assert (await scrub_number("+15550009999", provider=_provider(), settings=settings, now=NOW)).status == "exempt"


def test_parse_test_allowlist():
    assert parse_test_allowlist("") == frozenset()
    assert parse_test_allowlist(None) == frozenset()
    assert parse_test_allowlist(" +13135550199 , +15550001111 ") == {"+13135550199", "+15550001111"}


# ── (b) a non-allowlisted number still fails closed ──────────────────────────

async def test_non_allowlisted_number_falls_through_to_the_provider_and_is_unknown():
    result = await scrub_number(STRANGER, provider=_provider(), settings=_Settings(TEST_NUMBER), now=NOW)
    assert result.status == "unknown"
    assert not is_dialable(result.status)


async def test_empty_allowlist_never_exempts_anything():
    result = await scrub_number(TEST_NUMBER, provider=_provider(), settings=_Settings(""), now=NOW)
    assert result.status == "unknown"
    assert not is_dialable(result.status)


async def test_unknown_lead_is_refused_by_the_enqueue_gate(monkeypatch):
    """A stranger scrubbing to unknown must never reach the dial queue."""
    import workers.brain as brain
    from contextlib import asynccontextmanager
    import uuid

    lead_id = uuid.uuid4()

    class _Lead:
        id = lead_id
        phone_e164 = STRANGER
        national_dnc_status = "unknown"

    enqueued: list = []

    class _LeadRepo:
        def __init__(self, s): pass
        async def get_by_id(self, _id): return _Lead()

    class _DQRepo:
        def __init__(self, s): pass
        async def enqueue(self, **kw): enqueued.append(kw)

    @asynccontextmanager
    async def _scope():
        yield object()

    monkeypatch.setattr(brain, "session_scope", _scope)
    monkeypatch.setattr(brain, "LeadRepo", _LeadRepo)
    monkeypatch.setattr(brain, "DialQueueRepo", _DQRepo)

    worker = brain.WorkersBrain()
    try:
        await worker.enqueue_call(_Lead(), "default")
    finally:
        await worker._ai_agent.aclose()

    assert enqueued == [], "an unknown lead must not be queued"


# ── ScrubResult invariant: providers still cannot fabricate exempt ───────────

def test_a_provider_still_cannot_return_exempt():
    with pytest.raises(ValueError, match="exempt"):
        ScrubResult(status="exempt", provider="x", checked_at=NOW)


def test_operator_exempt_is_the_only_way_to_build_an_exempt_result():
    r = ScrubResult.operator_exempt(NOW, "test_allowlist")
    assert r.status == "exempt"
    assert r.source == "test_allowlist"


# ── (c) the boot guard forbids the allowlist in production ───────────────────

def test_boot_guard_refuses_production_with_a_non_empty_allowlist():
    settings = Settings(
        _env_file=None,
        app_env="production",
        bypass_tcpa=False,
        dnc_test_allowlist=TEST_NUMBER,
        twilio_signature_validation_enabled=True,
        eastwood_api_key="a-production-api-key",
    )
    with pytest.raises(UnsafeProductionConfigError, match="DNC_TEST_ALLOWLIST"):
        assert_production_safety(settings)


def test_boot_guard_allows_production_with_an_empty_allowlist():
    assert_production_safety(Settings(
        _env_file=None,
        app_env="production",
        bypass_tcpa=False,
        dnc_test_allowlist="",
        twilio_signature_validation_enabled=True,
        eastwood_api_key="a-production-api-key",
    ))


def test_boot_guard_allows_the_allowlist_outside_production():
    assert_production_safety(Settings(_env_file=None, app_env="development", bypass_tcpa=False, dnc_test_allowlist=TEST_NUMBER))
