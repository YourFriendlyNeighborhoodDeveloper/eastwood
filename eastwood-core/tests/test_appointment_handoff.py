"""
Appointment handoff brief.

When an appointment books, the licensed agent gets one SMS brief built from the
ProspectProfile the voice agent assembled — sent only after the write succeeds,
never for a failed booking, never twice, and never blocking the booking when the
destination is missing.
"""
from __future__ import annotations

import uuid

import pytest

import api.routes as routes
from domain.notifications import AppointmentNotificationService, format_agent_brief
from domain.notifications.appointment_notifier import AppointmentNotificationService as SVC

PROFILE = {
    "lead_id": "3f6c2b8e-9a41-4d6f-8f2b-7c1e5a904d33",
    "full_name": "Dolores Vega",
    "phone_e164": "+13135550123",
    "age": 68,
    "state": "MI",
    "coverage_purpose": "burial",
    "beneficiary_name": "Robert Vega",
    "beneficiary_relationship": "husband",
    "desired_coverage_amount": 15000,
    "monthly_budget": 60,
    "existing_coverage": "yes",
    "existing_coverage_amount": 5000,
    "motivation_summary": "want to cover my funeral so the kids don't have to",
    "health_summary": "diabetes",
    "medications_summary": "metformin",
    "tobacco_status": "none",
    "quote_amount": 15000,
    "quoted_monthly_premium": 62.5,
    "quote_type": "carrier",
    "carrier_name": "Mutual of Omaha",
    "appointment_time": "2026-07-15 10:00 ET",
    "objections": [{"objectionType": "price", "text": "feels a bit steep"}],
    "unanswered_questions": ["Confirm exact height and weight"],
    "transcript_summary": "Interested in burial coverage; quoted ~$62.50/mo; appointment booked.",
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


# ── The brief content ─────────────────────────────────────────────────────────

def test_brief_contains_every_required_section():
    brief = format_agent_brief(PROFILE)

    assert "NEW EASTWOOD APPOINTMENT" in brief
    assert "Dolores Vega" in brief                       # name
    assert "+13135550123" in brief                       # phone
    assert "2026-07-15 10:00 ET" in brief                # appointment time
    assert "Robert Vega (husband)" in brief              # beneficiary + relationship
    assert "Purpose: burial" in brief                    # purpose
    assert "$15,000" in brief                            # coverage wanted
    assert "$60/month" in brief                          # budget
    assert "yes ($5,000)" in brief                       # existing coverage
    assert "metformin" in brief                          # health / meds
    assert "$62.5" in brief and "Mutual of Omaha" in brief   # quote + carrier
    assert "cover my funeral" in brief                   # motivation
    assert "price: feels a bit steep" in brief           # objections
    assert "Confirm exact height and weight" in brief    # open questions


def test_brief_marks_an_estimate_as_carrier_tbd():
    p = {**PROFILE, "carrier_name": None}
    brief = format_agent_brief(p)
    assert "estimate, carrier TBD" in brief


def test_brief_never_invents_missing_fields():
    brief = format_agent_brief({"lead_id": "x", "phone_e164": "+15555550100"})
    assert "Purpose: unknown" in brief
    assert "Protecting: unknown" in brief
    assert "unknown" in brief  # missing fields read as unknown, not fabricated


# ── The notification service ──────────────────────────────────────────────────

def test_successful_send_delivers_one_sms():
    sms = _FakeSms()
    svc = AppointmentNotificationService(sms, "+13135559000")

    sent = svc.send_agent_brief(PROFILE, appointment_id="appt-1")

    assert sent is True
    assert len(sms.sent) == 1
    to, body = sms.sent[0]
    assert to == "+13135559000"
    assert "NEW EASTWOOD APPOINTMENT" in body


def test_missing_destination_does_not_send_and_does_not_raise():
    sms = _FakeSms()
    svc = AppointmentNotificationService(sms, "")  # no AGENT_NOTIFICATION_PHONE_E164

    sent = svc.send_agent_brief(PROFILE, appointment_id="appt-1")

    assert sent is False
    assert sms.sent == []


def test_duplicate_appointment_sends_only_one_sms():
    sms = _FakeSms()
    svc = AppointmentNotificationService(sms, "+13135559000")

    first = svc.send_agent_brief(PROFILE, appointment_id="appt-dup")
    second = svc.send_agent_brief(PROFILE, appointment_id="appt-dup")

    assert first is True
    assert second is False
    assert len(sms.sent) == 1


def test_sms_failure_is_soft():
    class _Boom:
        def send(self, to, body):
            raise RuntimeError("twilio down")

    svc = AppointmentNotificationService(_Boom(), "+13135559000")
    assert svc.send_agent_brief(PROFILE, appointment_id="appt-2") is False


# ── The route: send only after the write succeeds ─────────────────────────────

class _RecordingNotifier:
    def __init__(self):
        self.calls: list = []

    def send_agent_brief(self, profile, appointment_id=None):
        self.calls.append(appointment_id)
        return True


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class _FakeSession:
    """Minimal AsyncSession: records adds; commit succeeds unless told to fail."""

    def __init__(self, fail_commit=False):
        self.added = []
        self.committed = False
        self._fail_commit = fail_commit

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        # Assign ids the route reads back (slot.id, appointment.id).
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                try:
                    obj.id = uuid.uuid4()
                except Exception:
                    pass

    async def commit(self):
        if self._fail_commit:
            raise RuntimeError("db write failed")
        self.committed = True


def _body(with_profile=True):
    return {
        "lead_id": PROFILE["lead_id"],
        "session_id": "sess-1",
        "preferred_date": "2026-07-15",
        "preferred_time": "10:00",
        "prospect_profile": PROFILE if with_profile else None,
    }


async def test_route_sends_exactly_one_brief_on_successful_booking(monkeypatch):
    notifier = _RecordingNotifier()
    monkeypatch.setattr(routes, "_make_appointment_notifier", lambda s: notifier)

    async def _no_publish(*a, **k):
        return None
    monkeypatch.setattr(routes, "publish", _no_publish)

    session = _FakeSession()
    result = await routes.create_appointment(_FakeRequest(_body()), session)

    assert session.committed is True
    assert result["appointment_id"]
    assert len(notifier.calls) == 1, "exactly one brief on a successful booking"


async def test_route_sends_no_brief_when_the_write_fails(monkeypatch):
    notifier = _RecordingNotifier()
    monkeypatch.setattr(routes, "_make_appointment_notifier", lambda s: notifier)

    async def _no_publish(*a, **k):
        return None
    monkeypatch.setattr(routes, "publish", _no_publish)

    session = _FakeSession(fail_commit=True)
    with pytest.raises(RuntimeError):
        await routes.create_appointment(_FakeRequest(_body()), session)

    assert notifier.calls == [], "a failed booking must send no agent SMS"


async def test_route_persists_the_profile_record(monkeypatch):
    from db.models import HandoffProfile

    notifier = _RecordingNotifier()
    monkeypatch.setattr(routes, "_make_appointment_notifier", lambda s: notifier)

    async def _no_publish(*a, **k):
        return None
    monkeypatch.setattr(routes, "publish", _no_publish)

    session = _FakeSession()
    await routes.create_appointment(_FakeRequest(_body()), session)

    profiles = [o for o in session.added if isinstance(o, HandoffProfile)]
    assert len(profiles) == 1, "the handoff profile is persisted as the canonical record"
    assert profiles[0].profile["full_name"] == "Dolores Vega"
    assert profiles[0].profile["coverage_purpose"] == "burial"


async def test_route_still_books_without_a_profile(monkeypatch):
    notifier = _RecordingNotifier()
    monkeypatch.setattr(routes, "_make_appointment_notifier", lambda s: notifier)

    async def _no_publish(*a, **k):
        return None
    monkeypatch.setattr(routes, "publish", _no_publish)

    session = _FakeSession()
    result = await routes.create_appointment(_FakeRequest(_body(with_profile=False)), session)

    assert result["appointment_id"]
    assert notifier.calls == [], "no profile → no brief, but the booking succeeds"
