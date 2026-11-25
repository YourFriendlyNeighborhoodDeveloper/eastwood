"""Campaign controls without a database: calendar math, spend model, budget
verdicts, rate limiting, dial authorization, FTC fees, production guards."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import domain.spend as spend
from config.settings import Settings, UnsafeProductionConfigError, assert_production_safety
from domain.dialing import check_dial_eligibility
from domain.inventory import ftc_registry_cost
from domain.scheduling.local import AvailabilityConfigError, LocalCalendar, parse_availability
from workers.brain import SlidingWindowRate

DET = ZoneInfo("America/Detroit")


# ── Calendar ─────────────────────────────────────────────────────────────────

def test_availability_spec_parses():
    w = parse_availability("mon-fri 17:00-20:00; sat 09:00-12:00, 13:00-15:00")
    assert sorted(w) == [0, 1, 2, 3, 4, 5]
    assert len(w[5]) == 2 and 6 not in w


@pytest.mark.parametrize("spec", ["someday 9:00-10:00", "mon 10:00-09:00", "mon nine-ten"])
def test_bad_availability_is_rejected(spec):
    with pytest.raises(AvailabilityConfigError):
        parse_availability(spec)


def _cal(**kw):
    args = dict(windows=parse_availability("mon-fri 17:00-20:00; sat 09:00-11:00"), tz=DET, slot_minutes=30,
                buffer_minutes=0, max_per_day=4, min_notice_hours=2, horizon_days=7)
    args.update(kw)
    return LocalCalendar(**args)


def test_slots_respect_windows_notice_and_horizon():
    now = datetime(2026, 11, 24, 21, 30, tzinfo=timezone.utc)  # Tue 16:30 Detroit
    starts = _cal().candidate_starts(now)
    first = starts[0].astimezone(DET)
    assert (first.date(), first.hour, first.minute) == (date(2026, 11, 24), 18, 30), "2h notice"
    assert all(s.astimezone(DET).weekday() != 6 for s in starts), "no Sunday"
    sat = [s for s in starts if s.astimezone(DET).weekday() == 5]
    assert [s.astimezone(DET).strftime("%H:%M") for s in sat] == ["09:00", "09:30", "10:00", "10:30"]
    assert all(s.astimezone(DET).strftime("%H:%M") <= "19:30" for s in starts if s.astimezone(DET).weekday() < 5)


def test_buffer_spaces_slots_and_blackouts_skip_days():
    now = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
    cal = _cal(buffer_minutes=15, blackouts=frozenset({date(2026, 11, 25)}))
    starts = [s.astimezone(DET) for s in cal.candidate_starts(now)]
    tue = [s.strftime("%H:%M") for s in starts if s.date() == date(2026, 11, 24)]
    assert tue == ["17:00", "17:45", "18:30", "19:15"]
    assert not [s for s in starts if s.date() == date(2026, 11, 25)]


# ── Spend ────────────────────────────────────────────────────────────────────

def _settings(**kw):
    base = dict(_env_file=None, cost_voice_per_min_usd=0.014, cost_ai_per_min_usd=0.15,
                cost_transcribe_per_min_usd=0.006, cost_reserve_per_call_usd=1.75,
                outbound_daily_spend_limit_usd=25.0)
    base.update(kw)
    return Settings(**base)


def test_call_cost_rounds_voice_up_per_minute():
    lines = spend.call_cost_lines(61, _settings())
    assert lines == [("voice", 2.0, 0.014), ("ai", 1.0167, 0.15), ("transcribe", 1.0167, 0.006)]
    assert spend.call_cost_lines(0, _settings()) == [], "unanswered calls cost nothing"


def test_sms_segments():
    assert spend.sms_segments("x" * 153) == 1 and spend.sms_segments("x" * 154) == 2


@pytest.fixture
def budget(monkeypatch):
    state = {"today": 0.0, "campaign": 0.0, "campaign_today": 0.0}

    async def fake_spent(session, *, tenant_id, since=None, campaign_id=None):
        if campaign_id is None:
            return state["today"]
        return state["campaign_today"] if since is not None else state["campaign"]

    monkeypatch.setattr(spend, "spent", fake_spent)
    monkeypatch.setattr(spend, "get_settings", lambda: _settings())
    return state


def _campaign(budget_usd=50.0, daily=None):
    return SimpleNamespace(id="c1", budget_usd=budget_usd, daily_budget_usd=daily)


async def _verdict(**kw):
    args = dict(tenant_id="default", campaign=_campaign(), in_flight_total=0, in_flight_campaign=0)
    args.update(kw)
    return (await spend.check_budget(None, **args)).verdict


@pytest.mark.parametrize("state, kw, verdict", [
    ({}, {}, "ok"),
    ({"today": 23.5}, {}, "daily_exhausted"),
    ({"today": 20.0}, {"in_flight_total": 2}, "wait"),
    ({"campaign": 48.5}, {}, "campaign_exhausted"),
    ({"campaign": 45.0}, {"in_flight_campaign": 2, "in_flight_total": 2}, "wait"),
])
async def test_budget_verdicts(budget, state, kw, verdict):
    budget.update(state)
    assert await _verdict(**kw) == verdict


async def test_campaign_daily_budget(budget):
    budget.update({"campaign_today": 9.0})
    assert await _verdict(campaign=_campaign(daily=10.0)) == "daily_exhausted"


# ── Rate limit ───────────────────────────────────────────────────────────────

def test_sliding_window_rate():
    r, t0 = SlidingWindowRate(), datetime(2026, 11, 24, tzinfo=timezone.utc)
    for i in range(3):
        assert r.allow("c", 3, t0 + timedelta(seconds=i))
        r.record("c", t0 + timedelta(seconds=i))
    assert not r.allow("c", 3, t0 + timedelta(seconds=30))
    assert r.allow("c", 3, t0 + timedelta(seconds=61)), "window slides"
    assert r.allow("other", 3, t0), "keys are independent"


# ── Dial authorization ───────────────────────────────────────────────────────

class _S:
    class _R:
        def scalar_one_or_none(self):
            return None

    async def execute(self, *a, **k):
        return self._R()


def _lead(**kw):
    base = dict(national_dnc_status="clear", status="queued", tenant_id="default", phone_e164="+13135550101",
                contact_authorization={"ai_voice": True, "sms": False})
    base.update(kw)
    return SimpleNamespace(**base)


async def test_ai_voice_authorization_is_required_to_dial():
    assert await check_dial_eligibility(_S(), _lead()) == (True, "eligible")
    assert await check_dial_eligibility(_S(), _lead(contact_authorization=None)) == (False, "not_authorized:ai_voice")
    assert await check_dial_eligibility(_S(), _lead(contact_authorization={"ai_voice": False})) == (False, "not_authorized:ai_voice")


async def test_sms_needs_its_own_authorization_and_quarantine_never_dials():
    assert await check_dial_eligibility(_S(), _lead(), channel="sms") == (False, "not_authorized:sms")
    assert (await check_dial_eligibility(_S(), _lead(status="quarantined")))[1] == "lead_status:quarantined"


# ── FTC fees ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("n, fee", [(0, 0), (5, 0), (6, 85), (33, 2380), (59, 4590), (400, 23425)])
def test_ftc_registry_cost(n, fee):
    assert ftc_registry_cost(n) == fee


# ── Production guards ────────────────────────────────────────────────────────

def _prod(**kw):
    base = dict(_env_file=None, app_env="production", eastwood_api_key="k", dnc_scrub_provider="ftc_registry",
                dnc_san="s", dnc_area_codes="313", public_base_url="https://calls.example.com",
                consent_authorized_sellers="Example Agency")
    base.update(kw)
    return Settings(**base)


@pytest.mark.parametrize("kw, match", [
    ({"telephony_provider": "simulated"}, "TELEPHONY_PROVIDER"),
    ({"outbound_daily_spend_limit_usd": 0}, "OUTBOUND_DAILY_SPEND_LIMIT_USD"),
    ({"consent_authorized_sellers": ""}, "CONSENT_AUTHORIZED_SELLERS"),
])
def test_production_refuses_unsafe_campaign_settings(kw, match):
    with pytest.raises(UnsafeProductionConfigError, match=match):
        assert_production_safety(_prod(**kw))


def test_production_baseline_boots():
    assert_production_safety(_prod())



# ── Hard budget bounds ───────────────────────────────────────────────────────

def test_reserve_covers_a_full_length_call():
    s = _settings(max_call_minutes=15, cost_reserve_per_call_usd=1.75)
    assert s.call_reserve_usd() == pytest.approx(15 * (0.014 + 0.15 + 0.006))
    worst = sum(q * u for _, q, u in spend.call_cost_lines(15 * 60, s))
    assert worst <= s.call_reserve_usd() + 1e-9, "no call the provider allows can cost more than its reserve"
    assert _settings(max_call_minutes=1, cost_reserve_per_call_usd=1.75).call_reserve_usd() == 1.75


def test_call_length_cap_is_validated():
    with pytest.raises(Exception):
        _settings(max_call_minutes=0)


async def test_simulated_provider_honours_the_time_limit(tmp_path, monkeypatch):
    from clients.simulated_telephony import SimulatedTelephony

    monkeypatch.setattr(SimulatedTelephony, "_LEDGER", str(tmp_path / "calls.jsonl"))
    sim = SimulatedTelephony(_settings(public_base_url="http://x", twilio_auth_token="t"))
    monkeypatch.setattr(sim, "_complete", lambda *a, **k: _noop())
    monkeypatch.setattr(sim, "_plan", lambda: {"status": "completed", "answered_by": "human", "duration": 700,
                                              "behaviour": "quoted"})
    sid = sim.make_call("+13135550101", "http://x/twiml/call/s", "http://x/twilio/status/s", time_limit=600)
    assert sim._calls[sid]["duration"] == 600


async def _noop():
    return None
