"""
Formula quotes must never leave eastwood-core looking like a carrier rate.

FormulaAdapter premiums come from an exponential curve fitted to mid-market
benchmarks. No carrier has quoted them, none is obliged to honour them, and no
carrier has even been selected. Every boundary out of core carries the labels
that say so, and quote_records persists them.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from domain.quoting.adapters.formula import FormulaAdapter
from domain.quoting.base import CarrierQuote, QuoteRequest, QuoteResponse

ESTIMATE_LABELS = {
    "quote_type": "estimated",
    "rate_source": "formula_adapter",
    "carrier_rate": False,
    "bindable": False,
}


def _request(**overrides) -> QuoteRequest:
    base = dict(
        lead_id="lead-1",
        product_line="final_expense",
        age=68,
        state="MI",
        zip_code=None,
        county=None,
        face_value=1_000_000,  # cents == $10,000
        tobacco=False,
        gender=None,
        condition_codes=set(),
    )
    base.update(overrides)
    return QuoteRequest(**base)


def _quote(**overrides) -> CarrierQuote:
    base = dict(
        carrier="Mutual of Omaha",
        product_line="final_expense",
        monthly_premium=52.3,
        face_value=1_000_000,
        rate_tier="level",
    )
    base.update(overrides)
    return CarrierQuote(**base)


# ── 1. FormulaAdapter output is labelled an estimate ──────────────────────────

async def test_formula_adapter_quotes_carry_quote_type_estimated():
    quotes = await FormulaAdapter().get_quotes(_request())

    assert quotes, "the formula adapter must return quotes for a valid request"
    for q in quotes:
        assert q.source == "formula"
        assert q.quote_type == "estimated"
        assert q.rate_source == "formula_adapter"
        assert q.carrier_rate is False
        assert q.bindable is False
        assert q.labels() == ESTIMATE_LABELS


def test_carrier_sourced_quotes_are_carrier_rates_but_still_not_bindable():
    """Eastwood submits no application, so nothing it quotes is bindable."""
    for source in ("rate_table", "cms_api"):
        q = _quote(source=source)
        assert q.quote_type == "carrier"
        assert q.carrier_rate is True
        assert q.bindable is False, f"{source} must not claim to be bindable"


def test_unrecognised_source_fails_safe_to_an_estimate():
    """A new adapter cannot accidentally present itself as a carrier rate."""
    q = _quote(source="some_future_adapter")
    assert q.quote_type == "estimated"
    assert q.rate_source == "unknown"
    assert q.carrier_rate is False
    assert q.bindable is False


def test_empty_quote_response_is_labelled_an_estimate():
    empty = QuoteResponse(quotes=[], source="formula", fetched_at="2026-07-09T00:00:00Z")
    assert empty.labels()["quote_type"] == "estimated"
    assert empty.labels()["bindable"] is False


# ── 2. The labels survive the HTTP boundary ───────────────────────────────────

class _EmptyResult:
    def scalars(self):
        return iter(())


class _FakeSession:
    """No carrier_rate_tables rows, so RateTableAdapter yields nothing and the
    formula adapter is the only source — the live configuration today."""

    async def execute(self, *args, **kwargs):
        return _EmptyResult()


class _FakeRequest:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def json(self) -> dict:
        return self._payload


async def test_api_quote_response_labels_every_option_as_an_estimate():
    from api.routes import get_quote

    body = {
        "lead_id": str(uuid.uuid4()),
        "product_line": "final_expense",
        "age": 68,
        "state": "MI",
        "face_value": 10_000,
        "tobacco": False,
        "condition_codes": [],
    }
    payload = await get_quote(_FakeRequest(body), _FakeSession())

    # Top level: what the caller acts on.
    assert payload["quote_type"] == "estimated"
    assert payload["rate_source"] == "formula_adapter"
    assert payload["carrier_rate"] is False
    assert payload["bindable"] is False

    # The chosen quote and every alternative carry their own labels.
    assert payload["best"]["quote_type"] == "estimated"
    assert payload["best"]["bindable"] is False
    assert payload["all_options"], "formula adapter should offer alternatives"
    for option in payload["all_options"]:
        assert option["quote_type"] == "estimated"
        assert option["rate_source"] == "formula_adapter"
        assert option["carrier_rate"] is False
        assert option["bindable"] is False


# ── 3. The labels are persisted in quote_data ─────────────────────────────────

class _RecordingQuoteRepo:
    created: list[tuple] = []

    def __init__(self, session) -> None:
        self._s = session

    async def create(self, lead_id, session_id, data):
        _RecordingQuoteRepo.created.append((lead_id, session_id, data))
        return object()


@asynccontextmanager
async def _fake_session_scope():
    yield object()


async def _persist(monkeypatch, quote_payload: dict) -> dict:
    import workers.brain as brain

    _RecordingQuoteRepo.created.clear()
    monkeypatch.setattr(brain, "session_scope", _fake_session_scope)
    monkeypatch.setattr(brain, "QuoteRepo", _RecordingQuoteRepo)

    worker = brain.WorkersBrain()
    try:
        await worker.on_agent_event(
            {
                "eventType": "quote_presented",
                "leadId": "3f6c2b8e-9a41-4d6f-8f2b-7c1e5a904d33",
                "sessionId": "sess-1",
                "quote": quote_payload,
            }
        )
    finally:
        await worker._ai_agent.aclose()

    assert len(_RecordingQuoteRepo.created) == 1
    return _RecordingQuoteRepo.created[0][2]


async def test_persisted_quote_data_records_estimated_and_non_bindable(monkeypatch):
    """The exact camelCase payload the ai-agent publishes for a formula quote."""
    data = await _persist(
        monkeypatch,
        {
            "productLine": "final_expense",
            "monthlyPremium": 52.3,
            "carrier": "Mutual of Omaha",
            "faceValue": 10_000,
            "quoteType": "estimated",
            "rateSource": "formula_adapter",
            "carrierRate": False,
            "bindable": False,
        },
    )

    assert data["quote_type"] == "estimated"
    assert data["rate_source"] == "formula_adapter"
    assert data["carrier_rate"] is False
    assert data["bindable"] is False
    assert data["monthly_premium"] == 52.3


async def test_persisted_quote_without_labels_is_assumed_non_bindable(monkeypatch):
    """An unlabelled payload gets the safe reading, not the flattering one."""
    data = await _persist(
        monkeypatch,
        {"productLine": "final_expense", "monthlyPremium": 52.3, "carrier": "X"},
    )

    assert data["quote_type"] == "estimated"
    assert data["rate_source"] == "unknown"
    assert data["carrier_rate"] is False
    assert data["bindable"] is False


async def test_persisted_carrier_quote_keeps_its_carrier_label(monkeypatch):
    data = await _persist(
        monkeypatch,
        {
            "productLine": "medicare_advantage",
            "monthlyPremium": 0.0,
            "carrier": "Aetna",
            "quoteType": "carrier",
            "rateSource": "cms_api",
            "carrierRate": True,
            "bindable": False,
        },
    )

    assert data["quote_type"] == "carrier"
    assert data["rate_source"] == "cms_api"
    assert data["carrier_rate"] is True
    assert data["bindable"] is False
