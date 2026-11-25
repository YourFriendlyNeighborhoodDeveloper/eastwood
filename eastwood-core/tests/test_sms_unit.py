"""SMS-first engine building blocks, without a database."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from config.settings import Settings, UnsafeProductionConfigError, assert_production_safety
from domain.quoting.rate_sheets import RateSheetError, _validate_meta, compute_premium, read_rate_rows
from domain.sms import playbook, policy
from domain.sms.nlu import interpret, merge, Interpretation
from domain.sms.segments import encoding, segments, to_gsm

AGENCY = "Eastwood Insurance"
CTX = {"first": "Pat", "agency": AGENCY, "agent": "our licensed agent", "product": "final expense (burial) insurance",
       "product_short": "final expense", "choices": "1, 2 or 3",
       "pending": "How old are you?", "question": "In the past 2 years, have you been diagnosed with cancer?",
       "carrier": "Test Carrier", "plan": "Test Plan", "option": "$10,000 for $51.71/mo", "times": "1) Tue Dec 1 at 5:00 PM ET",
       "when": "Tue Dec 1 at 5:00 PM ET"}


# ── Segments ─────────────────────────────────────────────────────────────────

def test_gsm_vs_ucs2_segments():
    assert segments("a" * 160) == 1 and segments("a" * 161) == 2 and segments("a" * 306) == 2
    assert encoding("Hi — there") == "UCS-2" and segments("Hi — " + "a" * 70) == 2
    assert encoding(to_gsm("It’s “great” — really…")) == "GSM-7"


# ── Playbook: every template is cheap, compliant and complete ────────────────

@pytest.mark.parametrize("key", sorted(playbook.T))
def test_every_template_is_gsm7_and_passes_policy(key):
    for variant in playbook.T[key]:
        text = to_gsm(variant.format(**CTX))
        assert encoding(text) == "GSM-7", f"{key}: non-GSM characters would double the cost"
        problems = policy.validate(text, allowed_amounts={Decimal("10000"), Decimal("51.71")},
                                   first_message=(key == "opener"), agency=AGENCY)
        assert problems == [], f"{key}: {problems}"
        assert segments(text) <= 3


def test_openers_identify_agency_ai_and_opt_out():
    for v in playbook.T["opener"]:
        t = v.format(**CTX).lower()
        assert AGENCY.lower() in t and "ai assistant" in t and "stop" in t


# ── Policy ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, problem", [
    ("Your rate is $49.99/mo", "amount_not_from_stored_quote:49.99"),
    ("Approval is guaranteed approval for everyone", "misleading_guarantee"),
    ("You can't be denied", "misleading_guarantee"),
    ("Medicare is free for you", "medicare_government_benefit"),
    ("Please text your social security number", "requests_sensitive_data"),
    ("Rates run $40 to $60 a month", "vague_premium_range"),
    ("Hi — there", "non_gsm_characters"),
])
def test_policy_blocks(text, problem):
    problems = policy.validate(text, allowed_amounts=set(), first_message=False, agency=AGENCY)
    assert any(p.startswith(problem) for p in problems), problems


def test_stored_quote_amounts_pass():
    assert policy.validate("1) $15,000: $70.15/mo", allowed_amounts={Decimal("15000"), Decimal("70.15")},
                           first_message=False, agency=AGENCY) == []


def test_first_text_must_disclose():
    p = policy.validate("Hi Pat, want rates?", allowed_amounts=set(), first_message=True, agency=AGENCY)
    assert {"first_text_missing_agency", "first_text_missing_ai_disclosure", "first_text_missing_opt_out"} <= set(p)


# ── Understanding replies ────────────────────────────────────────────────────

D = date(2026, 11, 25)


@pytest.mark.parametrize("text, expecting, expect", [
    ("Yes please", "opt_in", {"intents": {"yes"}}),
    ("No thanks, not interested.", "opt_in", {"intents": {"decline"}}),
    ("wrong number", None, {"intents": {"wrong_number"}}),
    ("I'm 67", "age", {"values": {"age": 67}}),
    ("67", "age", {"values": {"age": 67}}),
    ("03/04/1955", "age", {"values": {"age": 71}}),
    ("no tobacco", "tobacco", {"values": {"tobacco": False}}),
    ("yes", "tobacco", {"values": {"tobacco": True}}),
    ("I quit smoking 5 years ago", "tobacco", {"values": {"tobacco": False}}),
    ("female", "sex", {"values": {"sex": "F"}}),
    ("twenty five thousand", "face", {"values": {"face": 25000}}),
    ("15", "face", {"values": {"face": 15000}}),
    ("$10,000", "face", {"values": {"face": 10000}}),
    ("not sure", "face", {"values": {"face_unsure": True}}),
    ("$50 a month", "budget", {"values": {"budget": 50}}),
    ("no", "budget", {"values": {"budget": 0}}),
    ("2", "option", {"values": {"option_choice": 2}}),
    ("1", "time", {"values": {"time_choice": 1}}),
    ("no", "health:major_2y", {"values": {"health:major_2y": False}}),
    ("Can I reschedule?", "booked", {"intents": {"reschedule"}}),
    ("C", "booked", {"intents": {"cancel"}}),
    ("Can you just call me?", None, {"intents": {"wants_call"}}),
])
def test_interpret(text, expecting, expect):
    it = interpret(text, expecting=expecting, offered=3, today=D)
    for k, v in expect.get("values", {}).items():
        assert it.values.get(k) == v, (text, it.values)
    assert expect.get("intents", set()) <= it.intents, (text, it.intents)


@pytest.mark.parametrize("text, objection, question", [
    ("That's too expensive", "price", None), ("I need to talk to my wife first", "spouse", None),
    ("I already have insurance", "have_coverage", None), ("Is this a scam?", "trust", None),
    ("Who is this?", None, "who"), ("how much would it cost?", None, "cost"), ("Is there a waiting period?", None, "exam"),
])
def test_objections_and_questions(text, objection, question):
    it = interpret(text, today=D)
    assert it.objection == objection and it.question == question


def test_quick_replies_never_need_the_model():
    for text, exp in (("yes", "opt_in"), ("2", "option"), ("67", "age"), ("no", "tobacco")):
        assert not interpret(text, expecting=exp, offered=3, today=D).unclear


def test_model_cannot_override_a_decline():
    rules = interpret("not interested", today=D)
    model = Interpretation(intents={"yes"}, values={"age": 70})
    assert merge(rules, model).intents == {"decline"}


# ── Rate sheets ──────────────────────────────────────────────────────────────

def test_premium_math_matches_rate_book_convention():
    # (24.00 x 10 + 36.00) x 0.0875 = 24.15
    assert compute_premium(rate_unit="per_1000_annual", rate=Decimal("24.00"), face=10000,
                           policy_fee_annual=Decimal("36"), modal_factor=Decimal("0.0875")) == Decimal("24.15")
    # rounding conventions differ: annual first vs once at the end
    a = compute_premium(rate_unit="per_1000_annual", rate=Decimal("24.333"), face=7000,
                        policy_fee_annual=Decimal("36"), modal_factor=Decimal("0.0875"), rounding="annual_then_modal")
    b = compute_premium(rate_unit="per_1000_annual", rate=Decimal("24.333"), face=7000,
                        policy_fee_annual=Decimal("36"), modal_factor=Decimal("0.0875"), rounding="final")
    assert a == Decimal("18.05") and b == Decimal("18.05")
    assert compute_premium(rate_unit="flat_monthly", rate=Decimal("42.5"), face=10000,
                           policy_fee_annual=Decimal("0"), modal_factor=Decimal("1")) == Decimal("42.50")


def _meta(**kw):
    m = {"carrier": "C", "product_line": "final_expense", "plan_name": "P", "states": ["MI"],
         "effective_date": "2026-01-01", "rate_unit": "per_1000_annual", "modal_factors": {"monthly": "0.0875"},
         "min_face": 2000, "max_face": 40000, "source_document": "Carrier rate book form 123"}
    m.update(kw)
    return m


@pytest.mark.parametrize("kw, match", [
    ({"states": ["ZZ"]}, "unsupported states"), ({"rate_unit": "per_hour"}, "rate_unit"),
    ({"modal_factors": {"annual": "1"}}, "monthly"), ({"expires_date": "2025-01-01"}, "after"),
    ({"source_document": ""}, "missing"), ({"min_face": 50000}, "face limits"),
])
def test_rate_sheet_metadata_is_validated(kw, match):
    with pytest.raises(RateSheetError, match=match):
        _validate_meta(_meta(**kw))


def test_rate_rows_are_validated(tmp_path):
    p = tmp_path / "r.csv"
    p.write_text("age,sex,tobacco,tier,rate\n65,F,N,level,24.00\n65,F,N,level,25.00\n")
    with pytest.raises(RateSheetError, match="duplicate"):
        read_rate_rows(p, "per_1000_annual")
    p.write_text("age,sex,tobacco,tier,rate\n65,X,N,level,24.00\n")
    with pytest.raises(RateSheetError, match="sex"):
        read_rate_rows(p, "per_1000_annual")


def test_production_refuses_test_rate_sheets():
    s = Settings(_env_file=None, app_env="production", eastwood_api_key="k", dnc_scrub_provider="ftc_registry",
                 dnc_san="s", dnc_area_codes="313", public_base_url="https://calls.example.com",
                 consent_authorized_sellers="Example Agency", allow_test_rate_sheets=True)
    with pytest.raises(UnsafeProductionConfigError, match="ALLOW_TEST_RATE_SHEETS"):
        assert_production_safety(s)


def test_slot_text_uses_the_prospects_timezone():
    from datetime import datetime, timezone
    start = datetime(2026, 12, 1, 22, 0, tzinfo=timezone.utc)  # 5 PM Eastern
    assert playbook.when(start, "MI") == "Tue Dec 1 at 5:00 PM ET"
    assert playbook.when(start, "WI") == "Tue Dec 1 at 4:00 PM CT"


# ── Cost: the texts every lead receives fit one segment when they can ─────────

def _engine_say(monkeypatch, agency, first, key, conv_id, product="final_expense"):
    from types import SimpleNamespace

    import config.settings as cs
    from domain.sms.engine import SmsEngine

    monkeypatch.setattr(cs, "_settings", cs.get_settings().model_copy(update={"agency_display_name": agency}))
    return SmsEngine(None)._say(SimpleNamespace(id=conv_id), SimpleNamespace(first_name=first, product_line=product),
                                {}, key, "o")


def test_opener_and_nudges_fit_one_segment(monkeypatch):
    from domain.sms import policy
    from domain.sms.segments import segments

    for i in range(40):                                   # every variant the picker can land on
        for product in ("final_expense", "life"):
            for key in ("opener", "followup_1", "followup_2"):
                text = _engine_say(monkeypatch, "Eastwood Insurance", "Patricia", key, f"c{i}", product)
                assert segments(text) == 1, (key, len(text), text)
            opener = _engine_say(monkeypatch, "Eastwood Insurance", "Patricia", "opener", f"c{i}", product)
            assert policy.validate(opener, allowed_amounts=set(), first_message=True, agency="Eastwood Insurance") == []


def test_a_long_agency_name_still_gets_a_valid_short_opener(monkeypatch):
    from domain.sms import policy
    from domain.sms.segments import segments

    agency = "Great Lakes Family Insurance Group of Southeast Michigan"
    text = _engine_say(monkeypatch, agency, "Bartholomew", "opener", "c1")
    assert segments(text) <= 2
    assert policy.validate(text, allowed_amounts=set(), first_message=True, agency=agency) == []



def test_choice_prompts_match_the_number_of_choices():
    assert playbook.choices(3) == "1, 2 or 3" and playbook.choices(2) == "1 or 2" and playbook.choices(1) == "1"


def test_delivery_receipts_are_requested_from_providers_that_support_them(monkeypatch):
    import config.settings as cs
    from domain.sms.engine import SmsEngine

    monkeypatch.setattr(cs, "_settings", cs.get_settings().model_copy(update={"public_base_url": "https://ew.example"}))

    class WithReceipts:
        def __init__(self):
            self.calls = []

        def send(self, to, body, status_callback=None):
            self.calls.append(status_callback)
            return "SM1"

    class Plain:
        def __init__(self):
            self.calls = []

        def send(self, to, body):
            self.calls.append(None)
            return "SM2"

    a, b = WithReceipts(), Plain()
    SmsEngine(a)._deliver("+13135550101", "hi", "m-1")
    SmsEngine(b)._deliver("+13135550101", "hi", "m-2")
    assert a.calls == ["https://ew.example/twilio/sms-status/m-1"] and b.calls == [None]
