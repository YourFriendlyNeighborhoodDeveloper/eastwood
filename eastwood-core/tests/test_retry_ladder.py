"""
Retry ladder and terminal session cleanup.

An unanswered call produces no call_ended, so Twilio's status callback is what
schedules the retry. Scheduling is idempotent against duplicate callbacks via the
dial_queue row's status, frees the concurrency slot immediately, closes the
ai-agent session, and never retries a suppressed / blocked / terminal lead.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from domain.dialing import RETRY_OFFSETS_HOURS, check_dial_eligibility, handle_terminal_status

SESSION_ID = "sess-abc-123"
LEAD_ID = uuid.uuid4()
NOW = datetime(2026, 7, 12, 15, 0, tzinfo=timezone.utc)


# ── Fakes modelling the persisted dial_queue semantics ────────────────────────

class _Entry:
    def __init__(self, *, status="dialing", attempt_count=0, max_attempts=5):
        self.id = uuid.uuid4()
        self.lead_id = LEAD_ID
        self.session_id = SESSION_ID
        self.status = status
        self.attempt_count = attempt_count
        self.max_attempts = max_attempts
        self.scheduled_at = NOW


class _Lead:
    def __init__(self, *, national_dnc_status="clear", status="dialing", suppressed=False):
        self.id = LEAD_ID
        self.tenant_id = "default"
        self.phone_e164 = "+13135550123"
        self.state = "MI"
        self.national_dnc_status = national_dnc_status
        self.status = status
        self._suppressed = suppressed


class _FakeDialQueueRepo:
    """Conditional transitions gate on status='dialing', exactly like the DB."""

    def __init__(self, session):
        self._entry = session.entry

    async def get_by_session(self, session_id):
        e = self._entry
        return e if e is not None and e.session_id == session_id else None

    async def count_active(self):
        return 1 if (self._entry and self._entry.status == "dialing") else 0

    async def mark_terminal_if_dialing(self, entry_id, status):
        if self._entry.status == "dialing":
            self._entry.status = status
            return True
        return False

    async def reschedule_if_dialing(self, entry_id, new_scheduled_at, new_attempt_count):
        if self._entry.status == "dialing":
            self._entry.status = "pending"
            self._entry.scheduled_at = new_scheduled_at
            self._entry.attempt_count = new_attempt_count
            return True
        return False


class _FakeLeadRepo:
    def __init__(self, session):
        self._lead = session.lead

    async def get_by_id(self, lead_id):
        return self._lead


class _FakeSuppressionRepo:
    def __init__(self, session):
        self._lead = session.lead

    async def is_suppressed(self, tenant_id, phone):
        return bool(getattr(self._lead, "_suppressed", False))


class _FakeSession:
    def __init__(self, entry, lead):
        self.entry = entry
        self.lead = lead
        self.commits = 0

    async def commit(self):
        self.commits += 1


class _AiAgent:
    def __init__(self, *, fail=False):
        self.calls: list[str] = []
        self._fail = fail

    async def end_session(self, session_id):
        self.calls.append(session_id)
        if self._fail:
            raise RuntimeError("agent unreachable")


@pytest.fixture(autouse=True)
def _wire_repos(monkeypatch):
    import repos.repos as repos_mod
    monkeypatch.setattr(repos_mod, "DialQueueRepo", _FakeDialQueueRepo)
    monkeypatch.setattr(repos_mod, "LeadRepo", _FakeLeadRepo)
    monkeypatch.setattr(repos_mod, "SuppressionRepo", _FakeSuppressionRepo)


async def _handle(entry, lead, call_status, ai_agent=None):
    session = _FakeSession(entry, lead)
    ai = ai_agent or _AiAgent()
    result = await handle_terminal_status(session, SESSION_ID, call_status, ai_agent=ai, now=NOW)
    return result, session, ai


# ── Retryable statuses schedule the first retry ───────────────────────────────

@pytest.mark.parametrize("twilio,expected", [("no-answer", "no_answer"), ("busy", "busy"), ("failed", "failed")])
async def test_retryable_status_schedules_the_first_retry(twilio, expected):
    entry = _Entry(attempt_count=0)
    result, _, _ = await _handle(entry, _Lead(), twilio)

    assert result["status"] == expected
    assert result["retry_scheduled"] is True
    assert result["attempt"] == 1
    assert entry.status == "pending"
    assert entry.attempt_count == 1


async def test_retry_offsets_match_the_configured_ladder():
    # Attempt just completed -> offset used.
    for attempt, expected_offset in enumerate(RETRY_OFFSETS_HOURS):
        entry = _Entry(attempt_count=attempt)
        result, _, _ = await _handle(entry, _Lead(), "no-answer")
        assert result["offset_hours"] == expected_offset, f"attempt {attempt}"
        assert result["retry_at"].startswith("2026-")


async def test_attempt_count_increments_exactly_once():
    entry = _Entry(attempt_count=2)
    await _handle(entry, _Lead(), "no-answer")
    assert entry.attempt_count == 3


# ── Idempotency against duplicate callbacks ───────────────────────────────────

async def test_duplicate_callback_schedules_exactly_one_retry():
    entry = _Entry(attempt_count=0)
    lead = _Lead()

    first, _, _ = await _handle(entry, lead, "no-answer")
    scheduled_at_after_first = entry.scheduled_at
    second, _, _ = await _handle(entry, lead, "no-answer")

    assert first["retry_scheduled"] is True
    assert second["retry_scheduled"] is False
    assert second["reason"] == "duplicate"
    assert entry.attempt_count == 1, "attempt incremented once"
    assert entry.scheduled_at == scheduled_at_after_first, "next_attempt_at not moved twice"


async def test_max_attempts_schedules_nothing_further():
    entry = _Entry(attempt_count=5, max_attempts=5)
    result, _, _ = await _handle(entry, _Lead(), "no-answer")

    assert result["retry_scheduled"] is False
    assert result["reason"] == "max_attempts"
    assert entry.status == "no_answer"       # terminal, slot freed
    assert entry.attempt_count == 5          # not incremented


# ── Non-retryable statuses ────────────────────────────────────────────────────

async def test_completed_schedules_no_retry():
    entry = _Entry()
    result, _, _ = await _handle(entry, _Lead(), "completed")
    assert result["retry_scheduled"] is False
    assert entry.status == "completed"


async def test_canceled_schedules_no_retry():
    entry = _Entry()
    result, _, _ = await _handle(entry, _Lead(), "canceled")
    assert result["retry_scheduled"] is False
    assert entry.status == "cancelled"


async def test_unrecognised_status_is_ignored():
    entry = _Entry()
    result, _, _ = await _handle(entry, _Lead(), "ringing")
    assert result["handled"] is False
    assert entry.status == "dialing"


# ── Eligibility: no retry for suppressed / blocked / terminal leads ───────────

async def test_suppressed_lead_schedules_no_retry():
    entry = _Entry()
    result, _, _ = await _handle(entry, _Lead(suppressed=True), "no-answer")
    assert result["retry_scheduled"] is False
    assert result["reason"] == "suppressed"
    assert entry.status == "no_retry"


@pytest.mark.parametrize("national", ["blocked", "unknown"])
async def test_national_dnc_blocked_or_unknown_schedules_no_retry(national):
    entry = _Entry()
    result, _, _ = await _handle(entry, _Lead(national_dnc_status=national), "no-answer")
    assert result["retry_scheduled"] is False
    assert result["reason"] == f"national_dnc:{national}"
    assert entry.status == "no_retry"


async def test_booked_or_terminal_lead_schedules_no_retry():
    entry = _Entry()
    result, _, _ = await _handle(entry, _Lead(status="appointment_set"), "no-answer")
    assert result["retry_scheduled"] is False
    assert result["reason"] == "lead_status:appointment_set"
    assert entry.status == "no_retry"


# ── Session cleanup and slot release ──────────────────────────────────────────

async def test_terminal_callback_calls_end_session():
    entry = _Entry()
    _, _, ai = await _handle(entry, _Lead(), "no-answer")
    assert ai.calls == [SESSION_ID]


async def test_end_session_failure_does_not_block_retry_persistence():
    entry = _Entry(attempt_count=0)
    result, session, ai = await _handle(entry, _Lead(), "no-answer", ai_agent=_AiAgent(fail=True))

    assert ai.calls == [SESSION_ID]
    assert result["end_session_ok"] is False
    assert result["retry_scheduled"] is True, "the retry persisted despite end_session failure"
    assert entry.status == "pending"
    assert session.commits == 1, "the queue state was committed before end_session ran"


async def test_concurrency_slot_drops_immediately_on_terminal_status():
    entry = _Entry()
    session = _FakeSession(entry, _Lead())
    assert await _FakeDialQueueRepo(session).count_active() == 1
    await handle_terminal_status(session, SESSION_ID, "no-answer", ai_agent=_AiAgent(), now=NOW)
    assert await _FakeDialQueueRepo(session).count_active() == 0


# ── The shared eligibility gate ───────────────────────────────────────────────

async def test_check_dial_eligibility_gates():
    async def eligible(lead) -> bool:
        # suppression is read from session.lead, so pair the session to the lead.
        return (await check_dial_eligibility(_FakeSession(_Entry(), lead), lead))[0]

    assert await eligible(_Lead()) is True
    assert await eligible(_Lead(national_dnc_status="blocked")) is False
    assert await eligible(_Lead(national_dnc_status="unknown")) is False
    assert await eligible(_Lead(status="dead")) is False
    assert await eligible(_Lead(suppressed=True)) is False
    assert await eligible(_Lead(national_dnc_status="exempt")) is True
