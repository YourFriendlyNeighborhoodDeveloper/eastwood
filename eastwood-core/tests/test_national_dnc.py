"""
National DNC Registry gate.

Nothing checked a purchased lead against the federal registry before dialing it.
The gate fails closed: a lead is dialable only when its clearance is an explicit
`clear` or `exempt`. `unknown` — the default for every imported lead — is not.

Distinct from the `suppressions` table, which records prospects who asked us
directly to stop calling. Both must hold.
"""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager

import pytest

from db.models import Lead
from domain.compliance import (
    BLOCKED,
    CLEAR,
    EXEMPT,
    UNKNOWN,
    DncScrubProvider,
    LocalCsvScrubProvider,
    ScrubResult,
    get_scrub_provider,
    is_dialable,
)

LEAD_ID = uuid.UUID("3f6c2b8e-9a41-4d6f-8f2b-7c1e5a904d33")
PHONE = "+13135550123"
BLOCKED_PHONE = "+13135559999"


class _Lead:
    def __init__(self, national_dnc_status: str = UNKNOWN) -> None:
        self.id = LEAD_ID
        self.phone_e164 = PHONE
        self.state = "MI"
        self.national_dnc_status = national_dnc_status


@asynccontextmanager
async def _fake_session_scope():
    yield object()


# ── The status vocabulary ─────────────────────────────────────────────────────

def test_only_clear_and_exempt_are_dialable():
    assert is_dialable(CLEAR)
    assert is_dialable(EXEMPT)
    assert not is_dialable(UNKNOWN)
    assert not is_dialable(BLOCKED)
    assert not is_dialable(None), "a lead with no status must not be dialable"


def test_a_provider_may_never_return_exempt():
    """`exempt` is an operator decision, never a scrub result."""
    from datetime import datetime, timezone

    with pytest.raises(ValueError, match="exempt"):
        ScrubResult(status=EXEMPT, provider="x", checked_at=datetime.now(timezone.utc))


# ── Import-time default ───────────────────────────────────────────────────────

def test_new_leads_default_to_unknown():
    """CSV import sets no clearance, so every imported lead lands unscrubbed."""
    column = Lead.__table__.c.national_dnc_status
    assert column.nullable is False
    assert column.default.arg == UNKNOWN
    assert UNKNOWN in str(column.server_default.arg)


# ── The local CSV provider ────────────────────────────────────────────────────

async def test_local_csv_provider_blocks_a_matching_number(tmp_path):
    blocklist = tmp_path / "national_dnc_blocked.local.csv"
    blocklist.write_text(f"# national registry export\n{BLOCKED_PHONE}\n(313) 555-0124\n")

    provider = LocalCsvScrubProvider(blocklist)
    result = await provider.scrub(BLOCKED_PHONE)

    assert result.status == BLOCKED
    assert result.provider == "local_csv"
    assert result.checked_at is not None
    assert "national_dnc_blocked.local.csv@" in result.source
    assert "2 entries" in result.source

    # Formatting in the file is tolerated; the lookup is on E.164.
    assert (await provider.scrub("+13135550124")).status == BLOCKED


async def test_local_csv_provider_returns_unknown_on_a_miss_never_clear(tmp_path):
    """Absence from a blocklist is not evidence of clearance."""
    blocklist = tmp_path / "b.csv"
    blocklist.write_text(f"{BLOCKED_PHONE}\n")

    result = await LocalCsvScrubProvider(blocklist).scrub(PHONE)

    assert result.status == UNKNOWN
    assert result.status != CLEAR


async def test_local_csv_provider_without_a_file_blocks_all_dialing(tmp_path):
    """No file means no knowledge, and no knowledge means no dial."""
    result = await LocalCsvScrubProvider(tmp_path / "missing.csv").scrub(PHONE)

    assert result.status == UNKNOWN
    assert result.source is None
    assert not is_dialable(result.status)


def test_provider_registry_rejects_an_unknown_provider():
    class _S:
        dnc_scrub_provider = "acme_registry"
        national_dnc_blocklist_path = "x.csv"

    with pytest.raises(ValueError, match="acme_registry"):
        get_scrub_provider(_S())


# ── The dial gates ────────────────────────────────────────────────────────────

class _FakeLeadRepo:
    lead: _Lead | None = None
    statuses: list = []
    dnc_writes: list = []

    def __init__(self, session) -> None:
        pass

    async def get_by_id(self, lead_id):
        return _FakeLeadRepo.lead

    async def update_status(self, lead_id, status):
        _FakeLeadRepo.statuses.append((lead_id, status))

    async def set_national_dnc(self, lead_id, status, provider=None, checked_at=None, source=None, audit=None):
        _FakeLeadRepo.dnc_writes.append(
            {"lead_id": lead_id, "status": status, "provider": provider, "source": source, "audit": audit}
        )


class _FakeDialQueueRepo:
    enqueued: list = []
    cancelled: list = []

    def __init__(self, session) -> None:
        pass

    async def enqueue(self, **kwargs):
        _FakeDialQueueRepo.enqueued.append(kwargs)

        class _Entry:
            id = uuid.uuid4()

        return _Entry()

    async def cancel_for_lead(self, lead_id):
        _FakeDialQueueRepo.cancelled.append(lead_id)


class _FakeFollowupRepo:
    cancelled: list = []

    def __init__(self, session) -> None:
        pass

    async def cancel_pending_for_lead(self, lead_id, sequence_type=None):
        _FakeFollowupRepo.cancelled.append(lead_id)


def _reset():
    _FakeLeadRepo.lead = None
    _FakeLeadRepo.statuses = []
    _FakeLeadRepo.dnc_writes = []
    _FakeDialQueueRepo.enqueued = []
    _FakeDialQueueRepo.cancelled = []
    _FakeFollowupRepo.cancelled = []


def _patch(monkeypatch):
    import workers.brain as brain

    monkeypatch.setattr(brain, "session_scope", _fake_session_scope)
    monkeypatch.setattr(brain, "LeadRepo", _FakeLeadRepo)
    monkeypatch.setattr(brain, "DialQueueRepo", _FakeDialQueueRepo)
    monkeypatch.setattr(brain, "FollowupRepo", _FakeFollowupRepo)
    return brain


async def _try_enqueue(monkeypatch, status: str) -> bool:
    """Returns True if the lead reached the dial queue."""
    brain = _patch(monkeypatch)
    _reset()
    _FakeLeadRepo.lead = _Lead(national_dnc_status=status)

    worker = brain.WorkersBrain()
    try:
        await worker.enqueue_call(_FakeLeadRepo.lead, "default", priority=0.9)
    finally:
        await worker._ai_agent.aclose()

    return len(_FakeDialQueueRepo.enqueued) == 1


async def test_unknown_status_never_enters_the_dial_queue(monkeypatch):
    assert await _try_enqueue(monkeypatch, UNKNOWN) is False


async def test_blocked_status_never_enters_the_dial_queue(monkeypatch):
    assert await _try_enqueue(monkeypatch, BLOCKED) is False


async def test_clear_status_enters_the_dial_queue(monkeypatch):
    assert await _try_enqueue(monkeypatch, CLEAR) is True


async def test_exempt_status_enters_the_dial_queue(monkeypatch):
    assert await _try_enqueue(monkeypatch, EXEMPT) is True


async def test_enqueue_reads_the_lead_fresh_not_the_callers_stale_copy(monkeypatch):
    """score_and_route holds an ORM copy from before the scrub's UPDATE."""
    brain = _patch(monkeypatch)
    _reset()
    _FakeLeadRepo.lead = _Lead(national_dnc_status=BLOCKED)  # what the DB says now
    stale = _Lead(national_dnc_status=CLEAR)                 # what the caller holds

    worker = brain.WorkersBrain()
    try:
        await worker.enqueue_call(stale, "default")
    finally:
        await worker._ai_agent.aclose()

    assert _FakeDialQueueRepo.enqueued == [], "the DB status must win"


# ── The dial loop is gated too, not just the queue ────────────────────────────

class _Entry:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.lead_id = LEAD_ID
        self.tenant_id = "default"
        self.attempt_count = 0


class _DialLoopQueueRepo(_FakeDialQueueRepo):
    due: list = []
    terminal: list = []
    dialing: list = []

    async def count_active(self):
        return 0

    async def get_due(self, now, limit=10):
        return _DialLoopQueueRepo.due

    async def mark_terminal(self, entry_id, status):
        _DialLoopQueueRepo.terminal.append((entry_id, status))

    async def mark_dialing(self, entry_id, session_id):
        _DialLoopQueueRepo.dialing.append(entry_id)


async def _drain_dial_queue(monkeypatch, status: str) -> tuple[int, list]:
    """Run one pass of process_dial_queue for a lead with the given clearance."""
    import domain.tcpa as tcpa_mod
    import workers.brain as brain

    _reset()
    _DialLoopQueueRepo.due = [_Entry()]
    _DialLoopQueueRepo.terminal = []
    _DialLoopQueueRepo.dialing = []
    _FakeLeadRepo.lead = _Lead(national_dnc_status=status)

    monkeypatch.setattr(brain, "session_scope", _fake_session_scope)
    monkeypatch.setattr(brain, "LeadRepo", _FakeLeadRepo)
    monkeypatch.setattr(brain, "DialQueueRepo", _DialLoopQueueRepo)
    monkeypatch.setattr(tcpa_mod, "in_tcpa_window", lambda *a, **k: True)
    # process_dial_queue now re-checks entity suppression via SuppressionRepo.
    import repos.repos as repos_mod

    class _NotSuppressed:
        def __init__(self, s): pass
        async def is_suppressed(self, tenant_id, phone): return False
    monkeypatch.setattr(repos_mod, "SuppressionRepo", _NotSuppressed)

    dialed: list = []
    worker = brain.WorkersBrain()

    async def _fake_initiate(lead, tenant_id, dial_queue_entry_id=None):
        dialed.append(lead.id)

    monkeypatch.setattr(worker, "_initiate_call", _fake_initiate)
    try:
        fired = await worker.process_dial_queue()
        # _initiate_call is dispatched with asyncio.create_task; let it run.
        await asyncio.sleep(0)
    finally:
        await worker._ai_agent.aclose()
    return fired, dialed


@pytest.mark.parametrize("status", [UNKNOWN, BLOCKED])
async def test_dial_loop_refuses_a_lead_without_clearance(monkeypatch, status):
    """An entry queued while clear can be blocked afterwards. Nothing dials unscrubbed."""
    fired, dialed = await _drain_dial_queue(monkeypatch, status)

    assert fired == 0
    assert dialed == []
    assert _DialLoopQueueRepo.terminal == [(_DialLoopQueueRepo.due[0].id, "compliance_blocked")]


@pytest.mark.parametrize("status", [CLEAR, EXEMPT])
async def test_dial_loop_dials_a_cleared_lead_when_other_gates_pass(monkeypatch, status):
    fired, dialed = await _drain_dial_queue(monkeypatch, status)

    assert fired == 1
    assert dialed == [LEAD_ID]
    assert _DialLoopQueueRepo.terminal == []


# ── Blocking cancels everything already scheduled ─────────────────────────────

async def test_blocked_lead_cancels_pending_retries_and_followups(monkeypatch):
    brain = _patch(monkeypatch)
    _reset()

    worker = brain.WorkersBrain()
    try:
        await worker._block_lead_national_dnc(object(), LEAD_ID)
    finally:
        await worker._ai_agent.aclose()

    assert _FakeLeadRepo.statuses == [(LEAD_ID, "compliance_blocked")]
    assert _FakeDialQueueRepo.cancelled == [LEAD_ID], "pending redials must be cancelled"
    assert _FakeFollowupRepo.cancelled == [LEAD_ID], "pending follow-up SMS must be cancelled"


# ── Scrub resolution ──────────────────────────────────────────────────────────

class _StubProvider(DncScrubProvider):
    name = "stub"

    def __init__(self, status: str) -> None:
        self._status = status

    async def scrub(self, phone_e164: str) -> ScrubResult:
        from datetime import datetime, timezone

        return ScrubResult(self._status, self.name, datetime.now(timezone.utc), "stub-v1")


class _ExplodingProvider(DncScrubProvider):
    name = "boom"

    async def scrub(self, phone_e164: str) -> ScrubResult:
        raise RuntimeError("registry unreachable")


async def _resolve(monkeypatch, provider, lead: _Lead) -> str:
    brain = _patch(monkeypatch)
    worker = brain.WorkersBrain()
    worker._dnc_scrub = provider
    try:
        return await worker._ensure_national_dnc_status(object(), lead)
    finally:
        await worker._ai_agent.aclose()


async def test_scrub_persists_the_decision_and_its_provenance(monkeypatch):
    _reset()
    status = await _resolve(monkeypatch, _StubProvider(BLOCKED), _Lead(UNKNOWN))

    assert status == BLOCKED
    assert len(_FakeLeadRepo.dnc_writes) == 1
    write = _FakeLeadRepo.dnc_writes[0]
    assert write["status"] == BLOCKED
    assert write["provider"] == "stub"
    assert write["source"] == "stub-v1"


async def test_a_decided_status_is_never_overwritten_by_a_scrub(monkeypatch):
    """An operator's `exempt` must survive a provider that has no opinion."""
    _reset()
    status = await _resolve(monkeypatch, _StubProvider(UNKNOWN), _Lead(EXEMPT))

    assert status == EXEMPT
    assert _FakeLeadRepo.dnc_writes == [], "no scrub should have run"


async def test_a_failing_provider_resolves_to_unknown_and_writes_nothing(monkeypatch):
    """The gate fails closed: a broken registry cannot cause a call."""
    _reset()
    status = await _resolve(monkeypatch, _ExplodingProvider(), _Lead(UNKNOWN))

    assert status == UNKNOWN
    assert not is_dialable(status)
    assert _FakeLeadRepo.dnc_writes == []
