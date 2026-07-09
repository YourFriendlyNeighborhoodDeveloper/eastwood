"""
Do-not-call handling.

capture_dnc published a dnc_request event that brain.on_agent_event had no
branch for. It fell through every elif and returned. SuppressionRepo.add was
called from nowhere, the suppressions table was never written, and a prospect
who asked to be removed stayed dialable on the next import.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

LEAD_ID = uuid.UUID("3f6c2b8e-9a41-4d6f-8f2b-7c1e5a904d33")
DIALED_PHONE = "+13135550123"


class _Lead:
    def __init__(self, phone: str = DIALED_PHONE) -> None:
        self.id = LEAD_ID
        self.phone_e164 = phone
        self.first_name = "Dolores"


class _FakeLeadRepo:
    lead: _Lead | None = _Lead()
    statuses: list[tuple] = []

    def __init__(self, session) -> None:
        pass

    async def get_by_id(self, lead_id):
        return _FakeLeadRepo.lead

    async def update_status(self, lead_id, status):
        _FakeLeadRepo.statuses.append((lead_id, status))


class _FakeSuppressionRepo:
    suppressed: set[tuple] = set()
    added: list[dict] = []

    def __init__(self, session) -> None:
        pass

    async def is_suppressed(self, tenant_id, phone_e164) -> bool:
        return (tenant_id, phone_e164) in _FakeSuppressionRepo.suppressed

    async def add(self, tenant_id, phone_e164, reason, notes=None):
        _FakeSuppressionRepo.added.append(
            {"tenant_id": tenant_id, "phone_e164": phone_e164, "reason": reason, "notes": notes}
        )
        _FakeSuppressionRepo.suppressed.add((tenant_id, phone_e164))


class _FakeDialQueueRepo:
    cancelled: list = []

    def __init__(self, session) -> None:
        pass

    async def cancel_for_lead(self, lead_id):
        _FakeDialQueueRepo.cancelled.append(lead_id)


@asynccontextmanager
async def _fake_session_scope():
    yield object()


def _reset():
    _FakeLeadRepo.lead = _Lead()
    _FakeLeadRepo.statuses = []
    _FakeSuppressionRepo.suppressed = set()
    _FakeSuppressionRepo.added = []
    _FakeDialQueueRepo.cancelled = []


async def _fire_dnc(monkeypatch, phone_reported_by_agent: str = DIALED_PHONE):
    import workers.brain as brain

    monkeypatch.setattr(brain, "session_scope", _fake_session_scope)
    monkeypatch.setattr(brain, "LeadRepo", _FakeLeadRepo)
    monkeypatch.setattr(brain, "SuppressionRepo", _FakeSuppressionRepo)
    monkeypatch.setattr(brain, "DialQueueRepo", _FakeDialQueueRepo)

    worker = brain.WorkersBrain()
    try:
        await worker.on_agent_event(
            {
                "eventType": "dnc_request",
                "leadId": str(LEAD_ID),
                "sessionId": "sess-1",
                "phoneE164": phone_reported_by_agent,
            }
        )
    finally:
        await worker._ai_agent.aclose()


async def test_dnc_request_suppresses_the_number(monkeypatch):
    _reset()
    await _fire_dnc(monkeypatch)

    assert len(_FakeSuppressionRepo.added) == 1, "the number must be added to suppressions"
    row = _FakeSuppressionRepo.added[0]
    assert row["phone_e164"] == DIALED_PHONE
    assert row["reason"] == "dnc_request"
    assert "sess-1" in row["notes"]


async def test_dnc_request_marks_the_lead_suppressed_and_cancels_the_dial_queue(monkeypatch):
    _reset()
    await _fire_dnc(monkeypatch)

    assert _FakeLeadRepo.statuses == [(LEAD_ID, "suppressed")]
    assert _FakeDialQueueRepo.cancelled == [LEAD_ID], "pending redials must be cancelled"


async def test_dnc_suppresses_the_dialled_number_not_the_one_the_model_reported(monkeypatch):
    """A misheard digit would suppress a stranger and leave this prospect dialable."""
    _reset()
    await _fire_dnc(monkeypatch, phone_reported_by_agent="+13135559999")

    assert len(_FakeSuppressionRepo.added) == 1
    assert _FakeSuppressionRepo.added[0]["phone_e164"] == DIALED_PHONE


async def test_dnc_request_is_idempotent(monkeypatch):
    """Suppressions has no unique constraint; a repeat must not add a second row."""
    _reset()
    await _fire_dnc(monkeypatch)
    await _fire_dnc(monkeypatch)

    assert len(_FakeSuppressionRepo.added) == 1
    assert _FakeLeadRepo.statuses == [(LEAD_ID, "suppressed"), (LEAD_ID, "suppressed")]


async def test_dnc_request_for_unknown_lead_suppresses_nothing(monkeypatch):
    _reset()
    _FakeLeadRepo.lead = None
    await _fire_dnc(monkeypatch)

    assert _FakeSuppressionRepo.added == []
    assert _FakeDialQueueRepo.cancelled == []


# ── The loop closes: a suppressed number is dropped on re-import ──────────────

async def test_suppressed_number_is_dropped_on_reimport(monkeypatch):
    """on_raw must not re-create a lead whose number is on the suppression list."""
    import workers.brain as brain
    import kafka_io.kafka_client as kafka_client

    _reset()
    _FakeSuppressionRepo.suppressed.add(("default", DIALED_PHONE))

    published: list = []

    async def _capture_publish(topic, payload, key=None):
        published.append(topic)

    class _ExplodingLeadRepo:
        def __init__(self, session) -> None:
            pass

        async def upsert_raw(self, *a, **k):
            raise AssertionError("a suppressed lead must never be upserted")

    monkeypatch.setattr(brain, "session_scope", _fake_session_scope)
    monkeypatch.setattr(brain, "SuppressionRepo", _FakeSuppressionRepo)
    monkeypatch.setattr(brain, "LeadRepo", _ExplodingLeadRepo)
    monkeypatch.setattr(kafka_client, "publish", _capture_publish)

    worker = brain.WorkersBrain()
    try:
        await worker.on_raw({"phone": DIALED_PHONE, "state": "MI", "source": "csv_import"})
    finally:
        await worker._ai_agent.aclose()

    assert published == [], "a suppressed lead must not reach leads.normalized"
