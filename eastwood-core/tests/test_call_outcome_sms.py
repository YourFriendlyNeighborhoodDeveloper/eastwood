"""
Per-call outcome notification to Ryan.

Every COMPLETED CONNECTED call sends exactly one outcome SMS, assembled from the
profile the voice agent captured — facts or 'unknown', never invented. Calls
that never connected publish no call_ended, so they send nothing and no profile
is fabricated. The destination comes from settings. Quote follow-up SMS uses
only the quote persisted from the call.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest

from domain.notifications import (
    CallOutcomeNotificationService,
    canonical_outcome,
    format_call_outcome,
    format_call_outcome_concise,
)
from domain.notifications.call_outcome_notifier import CallOutcomeNotificationService as SVC

LEAD_ID = "3f6c2b8e-9a41-4d6f-8f2b-7c1e5a904d33"

PROFILE = {
    "lead_id": LEAD_ID,
    "full_name": "Dolores Vega",
    "phone_e164": "+13135550123",
    "age": 68,
    "state": "MI",
    "rating_sex": "female",
    "employment_status": "social_security",
    "height_weight": "5'4\" 150 lbs",
    "coverage_purpose": "burial",
    "beneficiary_name": "Robert Vega",
    "beneficiary_relationship": "husband",
    "motivation_summary": "wants the funeral covered so the kids don't have to",
    "existing_coverage": "no",
    "desired_coverage_amount": 10000,
    "monthly_budget": 100,
    "health_summary": "diabetes",
    "medications_summary": "metformin",
    "tobacco_status": "none",
    "quote_options": [
        {"label": "C", "face_value": 10000, "monthly_premium": 55.2, "quote_type": "estimated"},
        {"label": "B", "face_value": 15000, "monthly_premium": 82.8, "quote_type": "estimated"},
        {"label": "A", "face_value": 25000, "monthly_premium": 138.0, "quote_type": "estimated"},
    ],
    "selected_option": "B",
    "affordability_confirmed": True,
    "quote_type": "estimated",
    "quote_source": "estimate",
    "carrier_name": None,
    "calendly_status": "confirmed",
    "appointment_time": "2026-07-16T14:00:00Z America/Detroit",
    "objections": [{"objectionType": "price", "text": "feels steep"}],
    "unanswered_questions": ["Confirm exact height and weight"],
    "transcript_summary": "Chose Option B; appointment booked.",
}


class _FakeSms:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send(self, to, body):
        self.sent.append((to, body))
        return "sid-1"


@pytest.fixture(autouse=True)
def _clear_dedupe():
    SVC._notified.clear()
    yield
    SVC._notified.clear()


# ── The SMS content ───────────────────────────────────────────────────────────

def test_outcome_sms_contains_every_required_section():
    body = format_call_outcome(PROFILE, "appointment_set")

    assert "EASTWOOD CALL COMPLETE" in body
    assert "Dolores Vega" in body and "+13135550123" in body
    assert "State: MI" in body
    assert "Age/DOB: 68" in body
    assert "Rating sex: female" in body
    assert "Employment/income: social_security" in body
    assert "Purpose: burial" in body
    assert "Robert Vega (husband)" in body
    assert "Why now: wants the funeral covered" in body
    assert "Coverage requested: $10,000" in body
    assert "Budget: $100/month" in body
    assert "metformin" in body and "diabetes" in body
    assert "Option C: $10,000 / $55.2/mo" in body
    assert "Option B: $15,000 / $82.8/mo" in body
    assert "Option A: $25,000 / $138.0/mo" in body
    assert "Selected: B" in body
    assert "Quote source: estimate" in body
    assert "Carrier: TBD" in body
    assert "Outcome: appointment_booked" in body
    assert "Calendly status: confirmed" in body
    assert "price: feels steep" in body
    assert "Confirm exact height and weight" in body
    assert "Chose Option B" in body


def test_unknown_fields_stay_unknown_never_invented():
    body = format_call_outcome({"lead_id": LEAD_ID, "phone_e164": "+15555550100"}, None)
    assert "Rating sex: unknown" in body
    assert "Employment/income: unknown" in body
    assert "Option C: none" in body
    assert "Selected: none" in body
    assert "Outcome: other" in body
    assert "Calendly status: not requested" in body


def test_outcomes_map_onto_the_canonical_vocabulary():
    assert canonical_outcome("appointment_set") == "appointment_booked"
    assert canonical_outcome("hangup") == "disconnected"
    assert canonical_outcome("dnc") == "DNC"
    assert canonical_outcome("transferred") == "transfer_requested"
    assert canonical_outcome("quoted_not_closed") == "quoted_not_closed"
    assert canonical_outcome("quote_failed") == "quote_failed"
    assert canonical_outcome("something_else") == "other"
    assert canonical_outcome(None) == "other"


def test_quote_failure_reason_is_reported():
    body = format_call_outcome({**PROFILE, "quote_failure_reason": "quote_service_unavailable",
                                "quote_options": [], "selected_option": None}, "quote_failed")
    assert "Quote failure: quote_service_unavailable" in body
    assert "Outcome: quote_failed" in body


def test_oversized_brief_falls_back_to_concise_with_profile_pointer():
    sms = _FakeSms()
    svc = CallOutcomeNotificationService(sms, "+13135550199")
    huge = {**PROFILE, "transcript_summary": "x" * 3000}

    assert svc.send_outcome(huge, session_id="sess-big", outcome="appointment_set") is True

    _, body = sms.sent[0]
    assert len(body) <= 1500
    assert "Full profile saved" in body, "nothing is silently discarded — the record is persisted"
    assert LEAD_ID in body


# ── The notification service ──────────────────────────────────────────────────

def test_exactly_one_sms_per_session_to_the_settings_destination():
    sms = _FakeSms()
    svc = CallOutcomeNotificationService(sms, "+13135550199")

    first = svc.send_outcome(PROFILE, session_id="sess-1", outcome="appointment_set")
    second = svc.send_outcome(PROFILE, session_id="sess-1", outcome="appointment_set")

    assert first is True and second is False
    assert len(sms.sent) == 1
    assert sms.sent[0][0] == "+13135550199"


def test_missing_destination_is_soft():
    sms = _FakeSms()
    svc = CallOutcomeNotificationService(sms, "")
    assert svc.send_outcome(PROFILE, session_id="s", outcome=None) is False
    assert sms.sent == []


def test_sms_failure_is_soft_and_retryable():
    class _Boom:
        def send(self, to, body):
            raise RuntimeError("twilio down")

    svc = CallOutcomeNotificationService(_Boom(), "+13135550199")
    assert svc.send_outcome(PROFILE, session_id="s2", outcome=None) is False
    # The failed send did not consume the dedupe slot.
    sms = _FakeSms()
    svc2 = CallOutcomeNotificationService(sms, "+13135550199")
    assert svc2.send_outcome(PROFILE, session_id="s2", outcome=None) is True


# ── brain.on_agent_event integration ─────────────────────────────────────────

class _FakeResult:
    def __init__(self, row=None):
        self._row = row

    def scalars(self):
        return self

    def first(self):
        return self._row


class _FakeSession:
    def __init__(self):
        self.added: list = []

    def add(self, obj):
        self.added.append(obj)

    async def execute(self, *a, **k):
        return _FakeResult(None)

    async def flush(self):
        pass


class _FakeLead:
    def __init__(self):
        self.id = uuid.UUID(LEAD_ID)
        self.first_name = "Dolores"
        self.phone_e164 = "+13135550123"
        self.health_data = {}


class _FakeLeadRepo:
    def __init__(self, session):
        pass

    async def get_by_id(self, lead_id):
        return _FakeLead()


class _RecordingFollowupRepo:
    created: list = []

    def __init__(self, session):
        pass

    async def create(self, lead_id, tenant_id, sequence_type, fire_at, context_data=None):
        _RecordingFollowupRepo.created.append((sequence_type, context_data))


def _brain(monkeypatch, fake_session: _FakeSession):
    import workers.brain as brain

    @asynccontextmanager
    async def _scope():
        yield fake_session

    monkeypatch.setattr(brain, "session_scope", _scope)
    monkeypatch.setattr(brain, "LeadRepo", _FakeLeadRepo)
    monkeypatch.setattr(brain, "FollowupRepo", _RecordingFollowupRepo)
    _RecordingFollowupRepo.created.clear()
    return brain.WorkersBrain()


def _call_ended(outcome, profile=PROFILE):
    msg = {
        "eventType": "call_ended",
        "leadId": LEAD_ID,
        "sessionId": "sess-42",
        "outcome": outcome,
    }
    if profile is not None:
        msg["prospectProfile"] = profile
    return msg


async def test_connected_call_persists_profile_and_sends_one_outcome_sms(monkeypatch):
    from db.models import HandoffProfile

    fake_session = _FakeSession()
    worker = _brain(monkeypatch, fake_session)
    sms = _FakeSms()
    worker._outcome_notifier = CallOutcomeNotificationService(sms, "+13135550199")

    try:
        await worker.on_agent_event(_call_ended("appointment_set"))
        # At-least-once delivery: the same event arrives again.
        await worker.on_agent_event(_call_ended("appointment_set"))
    finally:
        await worker._ai_agent.aclose()

    profiles = [o for o in fake_session.added if isinstance(o, HandoffProfile)]
    assert len(profiles) >= 1, "the canonical profile record is persisted"
    assert profiles[0].profile["selected_option"] == "B"
    assert profiles[0].session_id == "sess-42"

    assert len(sms.sent) == 1, "exactly one outcome SMS per connected call"
    assert sms.sent[0][0] == "+13135550199"
    assert "EASTWOOD CALL COMPLETE" in sms.sent[0][1]


async def test_disconnected_call_without_profile_sends_nothing(monkeypatch):
    from db.models import HandoffProfile

    fake_session = _FakeSession()
    worker = _brain(monkeypatch, fake_session)
    sms = _FakeSms()
    worker._outcome_notifier = CallOutcomeNotificationService(sms, "+13135550199")

    try:
        # A call_ended with no profile (or a no-answer that never publishes one)
        # fabricates nothing.
        await worker.on_agent_event(_call_ended("no_answer", profile=None))
    finally:
        await worker._ai_agent.aclose()

    assert sms.sent == [], "no profile, no SMS — nothing is invented"
    assert not any(isinstance(o, HandoffProfile) for o in fake_session.added)


async def test_dnc_call_notifies_ryan_but_schedules_no_sales_followup(monkeypatch):
    fake_session = _FakeSession()
    worker = _brain(monkeypatch, fake_session)
    sms = _FakeSms()
    worker._outcome_notifier = CallOutcomeNotificationService(sms, "+13135550199")

    try:
        await worker.on_agent_event(_call_ended("dnc"))
    finally:
        await worker._ai_agent.aclose()

    assert len(sms.sent) == 1, "Ryan still learns the outcome (internal, not sales)"
    assert "Outcome: DNC" in sms.sent[0][1]
    assert _RecordingFollowupRepo.created == [], "a DNC prospect gets no follow-up sequence"


async def test_quoted_not_closed_followup_uses_only_the_persisted_quote(monkeypatch):
    from config.settings import get_settings

    fake_session = _FakeSession()
    worker = _brain(monkeypatch, fake_session)
    sms = _FakeSms()
    worker._outcome_notifier = CallOutcomeNotificationService(sms, "+13135550199")

    try:
        await worker.on_agent_event(_call_ended("quoted_not_closed"))
    finally:
        await worker._ai_agent.aclose()

    assert _RecordingFollowupRepo.created, "quoted_not_closed schedules the follow-up sequence"
    _, context = _RecordingFollowupRepo.created[0]
    # The prospect chose Option B on the call — the SMS reuses exactly that.
    assert context["quote_amount"] == 82.8
    assert context["face_value"] == 15000
    assert context["quote_label"] == "estimate"
    assert context["carrier"] == "carrier TBD"
    assert context["calendly_link"] == get_settings().calendly_event_type_url

    # And the rendered SMS carries the persisted figures, honestly labelled.
    from domain.followup.sequences import render_template
    body = render_template("quoted_followup_day1", {"first_name": "Dolores", **context})
    assert "$15000" in body and "$82.8" in body
    assert "estimate" in body
    assert context["calendly_link"] in body
