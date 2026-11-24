"""Inbound SMS opt-out: a texted STOP suppresses the number for calls too.

Twilio's built-in opt-out only blocks further messages; it never tells
Eastwood, so without /twilio/sms a prospect who texted STOP stayed dialable.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

import config.settings as settings_module
import domain.compliance.sms_opt_out as opt_out
from app.main import app
from config.settings import Settings
from db.session import get_session
from domain.compliance.sms_opt_out import is_opt_out

PUBLIC_BASE = "https://eastwood.example.com"
TOKEN = "twilio_test_auth_token"


@pytest.mark.parametrize(
    "body",
    [
        "STOP", "stop", "Stop.", " STOP ", "Stop texting me", "STOPALL", "unsubscribe",
        "CANCEL", "end", "Quit", "revoke", "opt out", "Opt-Out", "please stop calling me",
        "Do not call this number again", "don't call me", "Don’t text me", "remove me from your list",
        "Take me off your list",
    ],
)
def test_opt_out_messages_are_recognised(body):
    assert is_opt_out(body)


@pytest.mark.parametrize(
    "body",
    ["YES", "Yes please call me", "What time is my appointment?", "", "   ", "START", "Can you call tomorrow"],
)
def test_ordinary_messages_are_not_opt_outs(body):
    assert not is_opt_out(body)


@pytest.fixture
def client(monkeypatch):
    s = Settings(
        _env_file=None, twilio_auth_token=TOKEN, public_base_url=PUBLIC_BASE,
        twilio_signature_validation_enabled=True, eastwood_api_key="k",
    )
    monkeypatch.setattr(settings_module, "_settings", s)
    calls: list = []

    class _Session:
        async def commit(self):
            calls.append("commit")

    async def _fake_apply(session, tenant_id, phone, *, note):
        calls.append((tenant_id, phone))
        return {"suppressed": True, "newly_suppressed": True, "leads": 1, "cancelled_dials": 1, "cancelled_followups": 2}

    async def _override():
        yield _Session()

    monkeypatch.setattr(opt_out, "apply_opt_out", _fake_apply)
    app.dependency_overrides[get_session] = _override
    c = TestClient(app)
    c.calls = calls
    try:
        yield c
    finally:
        app.dependency_overrides.pop(get_session, None)


def _signed_post(client, params: dict, token: str = TOKEN):
    sig = RequestValidator(token).compute_signature(f"{PUBLIC_BASE}/twilio/sms", params)
    return client.post("/twilio/sms", data=params, headers={"X-Twilio-Signature": sig})


def test_signed_stop_suppresses_the_normalized_sender(client):
    r = _signed_post(client, {"From": "+13135550142", "Body": "STOP", "MessageSid": "SM1"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml")
    assert client.calls == [("default", "+13135550142"), "commit"]


def test_non_opt_out_reply_changes_nothing(client):
    r = _signed_post(client, {"From": "+13135550142", "Body": "YES"})
    assert r.status_code == 200
    assert client.calls == []


def test_unsigned_or_forged_sms_webhook_is_refused(client):
    assert client.post("/twilio/sms", data={"From": "+13135550142", "Body": "STOP"}).status_code == 403
    assert _signed_post(client, {"From": "+13135550142", "Body": "STOP"}, token="wrong").status_code == 403
    assert client.calls == []
