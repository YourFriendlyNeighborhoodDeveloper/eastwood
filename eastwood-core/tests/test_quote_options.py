"""
Three-option (C/B/A) quote generation.

The engine — never the model — selects the faces, prices them, orders them, and
flags affordability. C solves the basic stated need, B adds a cushion, A is the
largest face the stated budget can carry. All faces stay inside configured
product limits, and every option carries the honesty labels.
"""
from __future__ import annotations

import pytest

from domain.quoting import FormulaAdapter, QuoteManager, QuoteRequest
from domain.quoting.options import build_quote_options, need_target, select_face_targets


def _request(age: int = 68, tobacco: bool = False) -> QuoteRequest:
    return QuoteRequest(
        lead_id="lead-1",
        product_line="final_expense",
        age=age,
        state="MI",
        zip_code=None,
        county=None,
        face_value=10_000 * 100,
        tobacco=tobacco,
        gender=None,
        condition_codes=set(),
    )


def _manager() -> QuoteManager:
    return QuoteManager(adapters=[FormulaAdapter()], timeout=5.0)


# ── Need targets ──────────────────────────────────────────────────────────────

def test_prospect_stated_amount_always_wins():
    assert need_target("burial", "burial", 12_000) == 12_000


def test_cremation_planning_target_defaults_to_10k():
    assert need_target(None, "cremation", None) == 10_000
    assert need_target("cremation", None, None) == 10_000


def test_burial_evaluates_a_higher_default_target():
    assert need_target("burial", None, None) == 15_000
    assert need_target(None, "burial", None) == 15_000


# ── Face selection ────────────────────────────────────────────────────────────

async def test_three_faces_are_ordered_c_below_b_below_a():
    targets = await select_face_targets(
        _request(),
        coverage_purpose="burial",
        burial_or_cremation="burial",
        desired_amount=10_000,
        monthly_budget=200.0,
        min_face=5_000,
        max_face=50_000,
    )
    assert targets["C"] < targets["B"] < targets["A"]
    assert targets["C"] == 10_000


async def test_budget_caps_option_a_at_the_largest_affordable_face():
    # At 68, the cheapest formula estimate is ≈ $6.2 per $1k/month, so a $100
    # budget affords $15k but not $20k.
    targets = await select_face_targets(
        _request(age=68),
        coverage_purpose="burial",
        burial_or_cremation=None,
        desired_amount=10_000,
        monthly_budget=100.0,
        min_face=5_000,
        max_face=50_000,
    )
    assert targets["A"] == 15_000, "A is the maximum the stated budget can carry"


async def test_no_budget_still_produces_a_reasonable_spread():
    targets = await select_face_targets(
        _request(),
        coverage_purpose="burial",
        burial_or_cremation=None,
        desired_amount=10_000,
        monthly_budget=None,
        min_face=5_000,
        max_face=50_000,
    )
    assert targets == {"C": 10_000, "B": 15_000, "A": 20_000}


async def test_faces_never_leave_configured_product_limits():
    low = await select_face_targets(
        _request(),
        coverage_purpose="burial",
        burial_or_cremation=None,
        desired_amount=2_000,  # below product minimum
        monthly_budget=None,
        min_face=5_000,
        max_face=50_000,
    )
    assert all(5_000 <= f <= 50_000 for f in low.values())
    assert low["C"] == 5_000, "never below the product minimum"

    high = await select_face_targets(
        _request(),
        coverage_purpose="burial",
        burial_or_cremation=None,
        desired_amount=80_000,  # above product maximum
        monthly_budget=None,
        min_face=5_000,
        max_face=50_000,
    )
    assert all(5_000 <= f <= 50_000 for f in high.values())


# ── Full option builds ────────────────────────────────────────────────────────

async def test_build_returns_three_options_in_presentation_order():
    options = await build_quote_options(
        _manager(),
        _request(),
        coverage_purpose="burial",
        burial_or_cremation="burial",
        desired_amount=10_000,
        monthly_budget=200.0,
        min_face=5_000,
        max_face=50_000,
    )
    assert [o.label for o in options] == ["C", "B", "A"]
    faces = [o.face_value_dollars for o in options]
    assert faces == sorted(faces), "C is the smallest, A the largest"
    for o in options:
        assert o.quote is not None
        assert o.quote.monthly_premium > 0
        assert o.within_budget is True


async def test_options_carry_honesty_labels_and_are_never_bindable():
    options = await build_quote_options(
        _manager(),
        _request(),
        coverage_purpose="cremation",
        burial_or_cremation="cremation",
        desired_amount=None,
        monthly_budget=None,
        min_face=5_000,
        max_face=50_000,
    )
    assert options, "cremation with no stated amount still quotes the default target"
    assert options[0].face_value_dollars == 10_000, "cremation planning target"
    for o in options:
        payload = o.to_payload()
        assert payload["quote_type"] == "estimated"
        assert payload["rate_source"] == "formula_adapter"
        assert payload["carrier_rate"] is False
        assert payload["bindable"] is False


async def test_an_unaffordable_stated_need_is_flagged_not_hidden():
    # Need $15k but only $70/month at 68 — the need option is presented and
    # flagged over budget, never silently replaced or inflated.
    options = await build_quote_options(
        _manager(),
        _request(age=68),
        coverage_purpose="burial",
        burial_or_cremation=None,
        desired_amount=15_000,
        monthly_budget=70.0,
        min_face=5_000,
        max_face=50_000,
    )
    assert len(options) == 1
    assert options[0].label == "C"
    assert options[0].face_value_dollars == 15_000
    assert options[0].within_budget is False


async def test_collapsed_faces_are_deduplicated_never_padded():
    # A tight budget collapses B/A onto C — the engine never invents spread.
    options = await build_quote_options(
        _manager(),
        _request(age=68),
        coverage_purpose="burial",
        burial_or_cremation=None,
        desired_amount=10_000,
        monthly_budget=100.0,
        min_face=5_000,
        max_face=50_000,
    )
    faces = [o.face_value_dollars for o in options]
    assert len(faces) == len(set(faces)), "no duplicate faces"
    assert all(o.face_value_dollars in (10_000, 15_000) for o in options)
