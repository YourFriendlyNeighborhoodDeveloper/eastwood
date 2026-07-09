"""
Guards on the observable sale path.

Two failures previously made a completed call invisible to the business:
  1. calls.quote_presented was published by the agent and consumed by nobody.
  2. the quote payload arrives camelCase, but QuoteRepo reads snake_case, so
     even once consumed the row persisted with a NULL premium.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from config.settings import Settings

# Verbatim payload shape published by ai-agent (see kafkaBridge.publishEvent and
# the get_quote tool handler). Keys are camelCase.
AGENT_QUOTE_PRESENTED: dict = {
    "eventType": "quote_presented",
    "leadId": "3f6c2b8e-9a41-4d6f-8f2b-7c1e5a904d33",
    "sessionId": "sess-abc-123",
    "quote": {
        "productLine": "final_expense",
        "monthlyPremium": 52.3,
        "carrier": "Mutual of Omaha",
        "faceValue": 10_000,
    },
}

# Every topic the agent produces to — mirrors topicForEvent() in kafkaBridge.ts,
# including the agent.events fallback for unmapped event types.
AGENT_PUBLISHED_TOPICS = {
    "agent.events",
    "calls.field_captured",
    "calls.quote_presented",
    "calls.objection_captured",
    "calls.consent_captured",
    "calls.dnc_requested",
    "calls.live_transfer_requested",
    "calls.ended",
}


class _RecordingQuoteRepo:
    """Captures what brain.on_agent_event hands to QuoteRepo.create."""

    created: list[tuple] = []

    def __init__(self, session) -> None:
        self._s = session

    async def create(self, lead_id, session_id, data):
        _RecordingQuoteRepo.created.append((lead_id, session_id, data))
        return object()


@asynccontextmanager
async def _fake_session_scope():
    yield object()


async def test_quote_presented_persists_normalized_fields(monkeypatch):
    """A quote presented on a call reaches QuoteRepo with usable snake_case fields."""
    import workers.brain as brain

    _RecordingQuoteRepo.created.clear()
    monkeypatch.setattr(brain, "session_scope", _fake_session_scope)
    monkeypatch.setattr(brain, "QuoteRepo", _RecordingQuoteRepo)

    worker = brain.WorkersBrain()
    try:
        await worker.on_agent_event(dict(AGENT_QUOTE_PRESENTED))
    finally:
        await worker._ai_agent.aclose()

    assert len(_RecordingQuoteRepo.created) == 1, "the quote must be persisted exactly once"
    lead_id, session_id, data = _RecordingQuoteRepo.created[0]

    assert lead_id == uuid.UUID(AGENT_QUOTE_PRESENTED["leadId"])
    assert session_id == "sess-abc-123"

    # QuoteRepo.create reads these keys. Before normalization they were all
    # absent, so quote_records rows persisted with NULL premium and face value.
    assert data["product_line"] == "final_expense"
    assert data["monthly_premium"] == 52.3
    assert data["face_value"] == 10_000
    assert data["carrier"] == "Mutual of Omaha"

    # The raw payload survives for the quote_data JSONB column.
    assert data["monthlyPremium"] == 52.3


async def test_quote_payload_normalization_is_idempotent():
    """A fully snake_case payload (e.g. replayed from core) passes through unchanged."""
    from workers.brain import _normalize_quote_payload

    snake = {
        "product_line": "final_expense",
        "monthly_premium": 41.0,
        "face_value": 5_000,
        "carrier": "Transamerica",
        "quote_type": "estimated",
        "rate_source": "formula_adapter",
        "carrier_rate": False,
        "bindable": False,
    }
    assert _normalize_quote_payload(snake) == snake


async def test_missing_quote_payload_does_not_crash():
    from workers.brain import _normalize_quote_payload

    normalized = _normalize_quote_payload({})
    assert normalized["product_line"] == ""
    assert normalized["monthly_premium"] is None
    assert normalized["face_value"] is None


def test_core_consumes_every_topic_the_agent_publishes():
    """A produced-but-unconsumed topic silently drops events. Catch it here."""
    consumed = set(Settings().consumed_topics())
    missing = AGENT_PUBLISHED_TOPICS - consumed
    assert not missing, f"ai-agent publishes to topics eastwood-core never consumes: {sorted(missing)}"
