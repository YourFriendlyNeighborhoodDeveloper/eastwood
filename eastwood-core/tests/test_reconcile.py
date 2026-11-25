"""Lost status callbacks: the reconciler asks the provider and settles the call."""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import domain.attempts as attempts
import domain.dialing.reconcile as rec


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


def _patch(monkeypatch, rows, calls):
    @asynccontextmanager
    async def scope():
        yield SimpleNamespace(execute=lambda *a, **k: _async(_Rows(rows)))

    async def terminal(session, session_id, status, *, ai_agent):
        calls.append(("terminal", session_id, status))
        return {}

    async def close(session, **kw):
        calls.append(("close", kw["session_id"], kw["status"], kw["duration_seconds"]))

    monkeypatch.setattr(rec, "session_scope", scope)
    monkeypatch.setattr(rec, "handle_terminal_status", terminal)
    monkeypatch.setattr(attempts, "close_attempt", close)


async def _async(v):
    return v


class _Provider:
    def __init__(self, states):
        self.states = states

    def fetch_call(self, sid):
        if self.states[sid] is None:
            raise RuntimeError("provider down")
        return self.states[sid]


async def test_ended_calls_are_settled_live_ones_left_alone(monkeypatch):
    calls: list = []
    _patch(monkeypatch, [("s1", "CA1"), ("s2", "CA2"), ("s3", "CA3")], calls)
    brain = SimpleNamespace(_sms=_Provider({
        "CA1": {"status": "completed", "duration": "95", "answered_by": "human"},
        "CA2": {"status": "in-progress", "duration": None},
        "CA3": None,
    }), _ai_agent=object())
    out = await rec.reconcile_stuck_calls(brain)
    assert out == {"checked": 3, "reconciled": 1}
    assert calls == [("terminal", "s1", "completed"), ("close", "s1", "completed", "95")]


async def test_no_provider_lookup_means_no_reconciliation(monkeypatch):
    out = await rec.reconcile_stuck_calls(SimpleNamespace(_sms=None, _ai_agent=None))
    assert out == {"checked": 0, "reconciled": 0}


def test_simulated_provider_records_survive_a_restart(tmp_path, monkeypatch):
    from clients.simulated_telephony import SimulatedTelephony
    from config.settings import Settings

    monkeypatch.setattr(SimulatedTelephony, "_LEDGER", str(tmp_path / "calls.jsonl"))
    s = Settings(_env_file=None, public_base_url="http://x", twilio_auth_token="t", simulated_call_seconds=0.05)
    sim = SimulatedTelephony(s)
    plan = {"status": "busy", "duration": 0, "delay": -1}
    sim._record("CAtest", plan)
    fresh = SimulatedTelephony(s)  # a "restarted" process
    assert fresh.fetch_call("CAtest")["status"] == "busy"
