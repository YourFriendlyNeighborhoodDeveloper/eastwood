"""Follow-up worker: every prospect-facing follow-up passes the dialer's gates.

Before this, run_due_tasks sent SMS with no check for opt-out, National DNC,
lead status, or the TCPA calling window, marked SMS tasks ``sent`` even when no
SMS client existed, and "call" tasks opened an ai-agent session that never
dialed. These tests pin the gated behaviour.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import db.session as db_session
import domain.dialing as dialing
import domain.outbound as outbound
import domain.tcpa as tcpa
import repos.repos as repos
from domain.followup import run_due_tasks

LEAD_ID = uuid.uuid4()
NEXT_WINDOW = datetime(2026, 11, 25, 13, 0, tzinfo=timezone.utc)


class _World:
    def __init__(self):
        self.tasks: list = []
        self.statuses: dict = {}
        self.rescheduled: dict = {}
        self.enqueued: list = []
        self.open_entry = False
        self.get_due_calls = 0
        self.lead = SimpleNamespace(
            id=LEAD_ID, tenant_id="default", phone_e164="+15555550142",
            first_name="Pat", state="MI", triage_priority=0.5,
        )


@pytest.fixture
def world(monkeypatch):
    w = _World()

    @asynccontextmanager
    async def _scope():
        yield object()

    class _FollowupRepo:
        def __init__(self, s): pass
        async def get_due(self, now, limit=100):
            w.get_due_calls += 1
            return w.tasks
        async def mark_sent(self, task_id): w.statuses[task_id] = "sent"
        async def mark_failed(self, task_id): w.statuses[task_id] = "failed"
        async def mark_status(self, task_id, status): w.statuses[task_id] = status
        async def reschedule(self, task_id, fire_at): w.rescheduled[task_id] = fire_at

    class _LeadRepo:
        def __init__(self, s): pass
        async def get_by_id(self, lead_id): return w.lead

    class _DialQueueRepo:
        def __init__(self, s): pass
        async def has_open_entry(self, lead_id): return w.open_entry
        async def enqueue(self, **kw): w.enqueued.append(kw)
        async def last_campaign_for_lead(self, lead_id): return None

    import domain.spend as spend
    w.spend = []

    async def _record_sms(session, **kw):
        w.spend.append(kw)
        return 0.013
    monkeypatch.setattr(spend, "record_sms", _record_sms)
    w.budget_ok = True

    async def _within(session, **kw):
        return w.budget_ok
    monkeypatch.setattr(spend, "sms_within_budget", _within)

    monkeypatch.setattr(db_session, "session_scope", _scope)
    monkeypatch.setattr(repos, "FollowupRepo", _FollowupRepo)
    monkeypatch.setattr(repos, "LeadRepo", _LeadRepo)
    monkeypatch.setattr(repos, "DialQueueRepo", _DialQueueRepo)
    w.set_gate = lambda permitted, why="permitted": monkeypatch.setattr(
        outbound, "outbound_permitted", _async_return((permitted, why))
    )
    w.set_eligible = lambda ok, why="eligible": monkeypatch.setattr(
        dialing, "check_dial_eligibility", _async_return((ok, why))
    )
    w.set_window = lambda inside: monkeypatch.setattr(tcpa, "in_tcpa_window", lambda *a, **k: inside)
    monkeypatch.setattr(tcpa, "next_tcpa_window_open", lambda *a, **k: NEXT_WINDOW)
    w.set_gate(True)
    w.set_eligible(True)
    w.set_window(True)
    return w


def _async_return(value):
    async def _f(*a, **k):
        return value
    return _f


def _task(channel="sms", template="missed_call_immediate"):
    return SimpleNamespace(
        id=uuid.uuid4(), lead_id=LEAD_ID, tenant_id="default",
        context_data={"_channel": channel, "_template": template},
    )


class _Sms:
    def __init__(self):
        self.sent: list = []

    def send(self, to, body):
        self.sent.append((to, body))
        return "SM_fake"


@pytest.mark.parametrize("why", ["halted", "never_authorized", "outbound_disabled"])
async def test_closed_campaign_gate_sends_nothing_and_leaves_tasks_pending(world, why):
    world.set_gate(False, why)
    world.tasks = [_task()]
    sms = _Sms()

    assert await run_due_tasks(sms) == 0
    assert sms.sent == []
    assert world.get_due_calls == 0, "tasks are not even read while halted"
    assert world.statuses == {}


@pytest.mark.parametrize("reason", ["suppressed", "national_dnc:blocked", "national_dnc:unknown", "lead_status:suppressed"])
async def test_ineligible_lead_is_blocked_not_sent(world, reason):
    world.set_eligible(False, reason)
    t = _task()
    world.tasks = [t]
    sms = _Sms()

    assert await run_due_tasks(sms) == 0
    assert sms.sent == []
    assert world.statuses == {t.id: "blocked"}


async def test_outside_tcpa_window_defers_to_next_window(world):
    world.set_window(False)
    t = _task()
    world.tasks = [t]
    sms = _Sms()

    assert await run_due_tasks(sms) == 0
    assert sms.sent == []
    assert world.rescheduled == {t.id: NEXT_WINDOW}
    assert world.statuses == {}, "a deferred task stays pending"


async def test_sms_without_a_client_is_skipped_not_marked_sent(world):
    t = _task()
    world.tasks = [t]

    assert await run_due_tasks(None) == 0
    assert world.statuses == {t.id: "skipped"}


async def test_eligible_sms_in_window_is_sent(world):
    t = _task(template="consent_request")
    world.tasks = [t]
    sms = _Sms()

    assert await run_due_tasks(sms) == 1
    assert len(sms.sent) == 1
    to, body = sms.sent[0]
    assert to == "+15555550142"
    assert "Reply STOP" in body and "Pat" in body
    assert world.statuses == {t.id: "sent"}
    assert len(world.spend) == 1, "every sent SMS is booked to the spend ledger"


async def test_callback_is_queued_for_the_dialer_not_dialed_directly(world):
    t = _task(channel="call", template="interested_busy_callback")
    world.tasks = [t]

    assert await run_due_tasks(_Sms()) == 1
    assert len(world.enqueued) == 1
    assert world.enqueued[0]["lead_id"] == LEAD_ID
    assert world.statuses == {t.id: "sent"}


async def test_callback_does_not_double_queue_a_lead(world):
    world.open_entry = True
    world.tasks = [_task(channel="call")]

    await run_due_tasks(_Sms())
    assert world.enqueued == []


async def test_consent_request_is_queued_as_a_gated_task_not_sent_inline(monkeypatch):
    """Routing to request_consent_sms must never text the lead directly."""
    import workers.brain as brain

    created: list = []

    @asynccontextmanager
    async def _scope():
        yield object()

    class _FollowupRepo:
        def __init__(self, s): pass
        async def create(self, **kw): created.append(kw)

    monkeypatch.setattr(brain, "session_scope", _scope)
    monkeypatch.setattr(brain, "FollowupRepo", _FollowupRepo)

    worker = brain.WorkersBrain()
    sms = _Sms()
    worker._sms = sms
    try:
        await worker._send_consent_sms(SimpleNamespace(id=LEAD_ID, tenant_id="default", phone_e164="+15555550142"))
    finally:
        await worker._ai_agent.aclose()

    assert sms.sent == []
    assert len(created) == 1
    assert created[0]["context_data"] == {"_channel": "sms", "_template": "consent_request"}
    assert created[0]["fire_at"] <= datetime.now(timezone.utc) + timedelta(seconds=1)


async def test_text_over_budget_is_deferred_to_tomorrow_not_sent(world):
    """Texts cost money too: past the daily ceiling or the campaign budget they
    wait (a raised budget lets them through), they are never sent over it."""
    world.budget_ok = False
    t = _task()
    world.tasks = [t]
    sms = _Sms()

    assert await run_due_tasks(sms) == 0
    assert sms.sent == [] and world.spend == []
    assert t.id in world.rescheduled and world.statuses == {}
