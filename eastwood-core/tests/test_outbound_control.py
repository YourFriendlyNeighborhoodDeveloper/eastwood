"""Outbound campaign control: the arm switch, authorization, and emergency halt.

Prospect-facing contact needs two keys — OUTBOUND_ENABLED in the environment
and a runtime ``resume`` recorded by an operator. A deployment that has never
been authorized is halted. Halt is always accepted; resume is refused (409)
while the deployment is disarmed.

Runs against the real app with an in-memory stand-in for the database session.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import config.settings as settings_module
from api.security import RoutePolicy, classify_route
from app.main import app
from config.settings import Settings
from db.session import get_session
from domain import outbound

API_KEY = "test-operational-api-key"
AUTH = {"Authorization": f"Bearer {API_KEY}"}


class _Result:
    def __init__(self, row):
        self._row = row

    def scalars(self):
        return self

    def first(self):
        return self._row


class _FakeSession:
    """Holds outbound_control_events in memory; execute() returns the latest."""

    def __init__(self):
        self.events: list = []
        self.commits = 0
        self._clock = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)

    def add(self, obj):
        self._clock += timedelta(seconds=1)
        obj.created_at = self._clock
        self.events.append(obj)

    async def flush(self):
        return None

    async def execute(self, _stmt):
        return _Result(self.events[-1] if self.events else None)

    async def commit(self):
        self.commits += 1


@pytest.fixture
def session():
    return _FakeSession()


@pytest.fixture
def armed(monkeypatch):
    def _apply(enabled: bool) -> Settings:
        s = Settings(_env_file=None, eastwood_api_key=API_KEY, outbound_enabled=enabled)
        monkeypatch.setattr(settings_module, "_settings", s)
        return s

    _apply(False)
    return _apply


@pytest.fixture
def client(session, armed):
    async def _override():
        yield session

    app.dependency_overrides[get_session] = _override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_session, None)


# ── Domain state machine ─────────────────────────────────────────────────────

async def test_disarmed_deployment_is_never_permitted_even_after_a_resume_row(session, armed):
    armed(True)
    await outbound.resume_outbound(session, operator="op", reason="test")
    armed(False)
    assert await outbound.outbound_permitted(session) == (False, outbound.DISARMED)


async def test_armed_but_never_authorized_is_halted(session, armed):
    armed(True)
    assert await outbound.outbound_permitted(session) == (False, outbound.NEVER_AUTHORIZED)


async def test_resume_then_halt_then_resume(session, armed):
    armed(True)
    await outbound.resume_outbound(session, operator="op", reason="launch")
    assert await outbound.outbound_permitted(session) == (True, outbound.PERMITTED)

    state = await outbound.halt_outbound(session, operator="op", reason="emergency")
    assert state.permitted is False and state.reason == outbound.HALTED
    assert state.operator == "op" and state.note == "emergency"

    await outbound.resume_outbound(session, operator="op2", reason="cleared")
    assert await outbound.outbound_permitted(session) == (True, outbound.PERMITTED)


async def test_resume_refused_while_disarmed_and_records_nothing(session, armed):
    with pytest.raises(outbound.OutboundNotArmedError):
        await outbound.resume_outbound(session, operator="op", reason="x")
    assert session.events == []


async def test_halt_is_accepted_while_disarmed(session, armed):
    state = await outbound.halt_outbound(session, operator="op", reason="belt and braces")
    assert state.permitted is False
    assert [e.action for e in session.events] == ["halt"]


def test_outbound_is_disarmed_by_default():
    assert Settings(_env_file=None).outbound_enabled is False


# ── API ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/api/outbound/status", "/api/outbound/halt", "/api/outbound/resume"])
def test_control_routes_require_the_api_key(path):
    assert classify_route(path) is RoutePolicy.AUTHENTICATED


def test_control_routes_reject_unauthenticated_calls(client):
    assert client.get("/api/outbound/status").status_code == 401
    assert client.post("/api/outbound/halt", json={"operator": "x", "reason": "y"}).status_code == 401


def test_status_reports_disarmed(client):
    r = client.get("/api/outbound/status", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["permitted"] is False
    assert r.json()["reason"] == "outbound_disabled"


def test_resume_returns_409_while_disarmed(client, session):
    r = client.post("/api/outbound/resume", headers=AUTH, json={"operator": "ops", "reason": "launch"})
    assert r.status_code == 409
    assert session.events == []


def test_operator_and_reason_are_required(client):
    for body in ({}, {"operator": "ops"}, {"reason": "why"}, {"operator": " ", "reason": "why"}):
        assert client.post("/api/outbound/halt", headers=AUTH, json=body).status_code == 422


def test_authorize_then_emergency_halt_over_http(client, session, armed):
    armed(True)
    r = client.post("/api/outbound/resume", headers=AUTH, json={"operator": "ops", "reason": "launch"})
    assert r.status_code == 200 and r.json()["permitted"] is True

    r = client.post("/api/outbound/halt", headers=AUTH, json={"operator": "ops", "reason": "stop now"})
    assert r.status_code == 200
    assert r.json() | {"changed_at": None} == {
        "permitted": False,
        "reason": "halted",
        "outbound_enabled": True,
        "last_action": "halt",
        "operator": "ops",
        "note": "stop now",
        "changed_at": None,
    }
    assert session.commits == 2
