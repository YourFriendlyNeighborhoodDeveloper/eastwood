"""Route security: Twilio webhook signature validation and operational API auth.

Twilio routes (/twiml/*, /twilio/*) must present a valid X-Twilio-Signature
computed over the externally visible URL — the one built from PUBLIC_BASE_URL,
which is exactly the base every callback URL handed to Twilio is built from.
Operational /api routes require `Authorization: Bearer <EASTWOOD_API_KEY>`.
Everything here runs against the real app with no database, Kafka, or network:
a denied request must be rejected before any backend is touched.
"""
from __future__ import annotations

import hashlib
import inspect
import logging

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

import api.security as security
import config.settings as settings_module
from api.security import RoutePolicy, classify_route, pinned_tenant_id, require_api_key
from app.main import app
from config.settings import Settings

PUBLIC_BASE = "https://eastwood.example.com"
TWILIO_TOKEN = "twilio_test_auth_token"
API_KEY = "test-operational-api-key"


def _make_settings(**overrides) -> Settings:
    kwargs: dict = dict(
        _env_file=None,
        app_env="development",
        twilio_auth_token=TWILIO_TOKEN,
        eastwood_api_key=API_KEY,
        public_base_url=PUBLIC_BASE,
        twilio_signature_validation_enabled=True,
        facebook_app_secret="",
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


@pytest.fixture
def use_settings(monkeypatch):
    """Install controlled settings; call again with overrides inside a test."""

    def _apply(**overrides) -> Settings:
        s = _make_settings(**overrides)
        monkeypatch.setattr(settings_module, "_settings", s)
        return s

    _apply()
    return _apply


@pytest.fixture
def client(use_settings) -> TestClient:
    return TestClient(app)


def twilio_sig(url: str, params: dict | None = None, token: str = TWILIO_TOKEN) -> str:
    return RequestValidator(token).compute_signature(url, params or {})


# ── Twilio webhook signature validation ──────────────────────────────────────


class TestTwilioSignature:
    def test_valid_form_signature_accepted(self, client):
        """A correctly signed form POST passes — with no Bearer token, proving
        Twilio routes never require the Eastwood API key. The signature is
        computed over the public HTTPS URL, not the internal test URL, proving
        validation reconstructs the externally visible address."""
        path = "/twiml/call/sess-123"
        params = {"CallSid": "CA123", "CallStatus": "in-progress"}
        sig = twilio_sig(f"{PUBLIC_BASE}{path}", params)
        r = client.post(path, data=params, headers={"X-Twilio-Signature": sig})
        assert r.status_code == 200
        assert "<Response>" in r.text

    def test_missing_signature_403(self, client):
        r = client.post("/twiml/call/sess-123", data={"CallSid": "CA123"})
        assert r.status_code == 403

    def test_invalid_signature_403(self, client):
        r = client.post(
            "/twiml/call/sess-123",
            data={"CallSid": "CA123"},
            headers={"X-Twilio-Signature": "bm90IGEgcmVhbCBzaWduYXR1cmU="},
        )
        assert r.status_code == 403

    def test_tampered_form_param_403(self, client):
        """The exact submitted form parameters are part of the signature."""
        path = "/twiml/call/sess-123"
        sig = twilio_sig(f"{PUBLIC_BASE}{path}", {"CallStatus": "completed"})
        r = client.post(
            path,
            data={"CallStatus": "failed"},  # differs from what was signed
            headers={"X-Twilio-Signature": sig},
        )
        assert r.status_code == 403

    def test_query_string_included(self, client):
        path = "/twiml/call/sess-123"
        good = twilio_sig(f"{PUBLIC_BASE}{path}?AccountSid=AC1")
        r = client.get(f"{path}?AccountSid=AC1", headers={"X-Twilio-Signature": good})
        assert r.status_code == 200

        # A signature that ignores the query string must not validate.
        bad = twilio_sig(f"{PUBLIC_BASE}{path}")
        r = client.get(f"{path}?AccountSid=AC1", headers={"X-Twilio-Signature": bad})
        assert r.status_code == 403

    def test_status_callback_requires_signature(self, client):
        """/twilio/status/* is rejected before its handler (and the database)
        is ever reached — no DB runs in this test suite."""
        r = client.post("/twilio/status/sess-9", data={"CallStatus": "completed"})
        assert r.status_code == 403

    def test_attacker_forwarded_headers_cannot_alter_validation(self, client):
        """With PUBLIC_BASE_URL set, client-supplied X-Forwarded-* headers are
        irrelevant: a signature over the attacker's origin fails, and a
        signature over the canonical origin still passes."""
        path = "/twiml/call/sess-123"
        spoof = {
            "X-Forwarded-Host": "evil.example.net",
            "X-Forwarded-Proto": "http",
        }

        evil_sig = twilio_sig(f"http://evil.example.net{path}", {"CallSid": "CA1"})
        r = client.post(
            path, data={"CallSid": "CA1"},
            headers={"X-Twilio-Signature": evil_sig, **spoof},
        )
        assert r.status_code == 403

        good_sig = twilio_sig(f"{PUBLIC_BASE}{path}", {"CallSid": "CA1"})
        r = client.post(
            path, data={"CallSid": "CA1"},
            headers={"X-Twilio-Signature": good_sig, **spoof},
        )
        assert r.status_code == 200

    def test_json_body_validates_via_body_hash(self, client):
        """Twilio JSON webhooks sign the URL carrying bodySHA256 — Twilio's
        documented JSON behavior, not form-parameter treatment."""
        body = '{"CallSid": "CA123"}'
        digest = hashlib.sha256(body.encode()).hexdigest()
        path = f"/twiml/call/sess-123?bodySHA256={digest}"
        sig = twilio_sig(f"{PUBLIC_BASE}{path}")
        r = client.post(
            "/twiml/call/sess-123",
            params={"bodySHA256": digest},
            content=body,
            headers={"X-Twilio-Signature": sig, "Content-Type": "application/json"},
        )
        assert r.status_code == 200

        # Same signature, different body — the hash no longer matches.
        r = client.post(
            "/twiml/call/sess-123",
            params={"bodySHA256": digest},
            content='{"CallSid": "CAtampered"}',
            headers={"X-Twilio-Signature": sig, "Content-Type": "application/json"},
        )
        assert r.status_code == 403

    def test_json_body_without_body_hash_403(self, client):
        path = "/twiml/call/sess-123"
        sig = twilio_sig(f"{PUBLIC_BASE}{path}")
        r = client.post(
            path,
            content='{"CallSid": "CA123"}',
            headers={"X-Twilio-Signature": sig, "Content-Type": "application/json"},
        )
        assert r.status_code == 403

    def test_unconfigured_auth_token_fails_closed(self, use_settings):
        use_settings(twilio_auth_token="")
        client = TestClient(app)
        path = "/twiml/call/sess-123"
        sig = twilio_sig(f"{PUBLIC_BASE}{path}")
        r = client.post(path, headers={"X-Twilio-Signature": sig})
        assert r.status_code == 403

    def test_disable_switch_is_explicit_and_logged(self, use_settings, caplog):
        """Development may disable validation only through the explicit
        setting, and every skipped request is loudly logged."""
        use_settings(twilio_signature_validation_enabled=False)
        client = TestClient(app)
        with caplog.at_level(logging.WARNING, logger="api.security"):
            r = client.post("/twiml/call/sess-123")  # no signature at all
        assert r.status_code == 200
        assert any("validation_disabled" in rec.getMessage() for rec in caplog.records)

    def test_denial_logs_no_secrets_or_payloads(self, client, caplog):
        """Denied requests log route, result, reason code, and request id —
        never the auth token, signature, body, or phone numbers."""
        sig = "c2lnbmF0dXJlLXZhbHVl"
        with caplog.at_level(logging.WARNING, logger="api.security"):
            client.post(
                "/twilio/status/sess-9",
                data={"CallStatus": "completed", "To": "+15551234567"},
                headers={"X-Twilio-Signature": sig},
            )
        messages = " ".join(rec.getMessage() for rec in caplog.records)
        assert "reason=invalid_signature" in messages
        assert TWILIO_TOKEN not in messages
        assert sig not in messages
        assert "+15551234567" not in messages


# ── Operational API authentication ───────────────────────────────────────────


class TestApiAuthentication:
    def test_missing_bearer_401(self, client):
        r = client.post("/api/triage/place", json={"health_data": {}})
        assert r.status_code == 401
        assert r.headers.get("WWW-Authenticate") == "Bearer"

    def test_malformed_authorization_401(self, client):
        r = client.post(
            "/api/triage/place", json={"health_data": {}},
            headers={"Authorization": f"Basic {API_KEY}"},
        )
        assert r.status_code == 401

    def test_invalid_bearer_403(self, client):
        r = client.post(
            "/api/triage/place", json={"health_data": {}},
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert r.status_code == 403

    def test_valid_bearer_succeeds(self, client):
        r = client.post(
            "/api/triage/place",
            json={"lead_id": "l1", "age": 70, "state": "MI", "health_data": {}},
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        assert r.status_code == 200
        assert "placement_ready" in r.json()

    def test_unconfigured_server_key_fails_closed(self, use_settings):
        use_settings(eastwood_api_key="")
        client = TestClient(app)
        r = client.post(
            "/api/triage/place", json={"health_data": {}},
            headers={"Authorization": "Bearer anything"},
        )
        assert r.status_code == 403

    def test_key_never_appears_in_response(self, client):
        for headers in ({}, {"Authorization": "Bearer wrong-key"}):
            r = client.post("/api/triage/place", json={}, headers=headers)
            assert API_KEY not in r.text

    def test_comparison_is_timing_safe(self):
        src = inspect.getsource(require_api_key)
        assert "compare_digest" in src
        assert "token == configured" not in src

    def test_health_is_public_and_reveals_nothing(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


# ── Facebook webhook (provider-signed) ───────────────────────────────────────


class TestFacebookWebhook:
    def test_unsigned_post_denied_when_secret_unconfigured(self, client):
        r = client.post("/api/webhooks/facebook", json={"entry": []})
        assert r.status_code == 403

    def test_valid_hub_signature_accepted(self, use_settings):
        import hmac as hmac_mod

        use_settings(facebook_app_secret="fb-test-secret")
        client = TestClient(app)
        body = b'{"entry": []}'
        digest = "sha256=" + hmac_mod.new(b"fb-test-secret", body, hashlib.sha256).hexdigest()
        r = client.post(
            "/api/webhooks/facebook",
            content=body,
            headers={"X-Hub-Signature-256": digest, "Content-Type": "application/json"},
        )
        assert r.status_code == 200


# ── Route policy table ───────────────────────────────────────────────────────


class TestRoutePolicy:
    @pytest.mark.parametrize(
        ("path", "policy"),
        [
            ("/health", RoutePolicy.PUBLIC),
            ("/metrics", RoutePolicy.PUBLIC),
            ("/twiml/call/abc", RoutePolicy.TWILIO_WEBHOOK),
            ("/twilio/status/abc", RoutePolicy.TWILIO_WEBHOOK),
            ("/api/webhooks/facebook", RoutePolicy.PROVIDER_SIGNED_WEBHOOK),
            ("/api/quote", RoutePolicy.AUTHENTICATED),
            ("/api/cockpit/queue", RoutePolicy.AUTHENTICATED),
            ("/something/unlisted", RoutePolicy.AUTHENTICATED),  # fail closed
        ],
    )
    def test_classification(self, path, policy):
        assert classify_route(path) is policy

    def test_every_registered_route_is_classified(self):
        """Only /health and /metrics may classify PUBLIC; every other route the
        app serves must land on an authenticated or signature-validated policy."""
        for route in app.routes:
            if not isinstance(route, APIRoute):
                continue
            policy = classify_route(route.path)
            if route.path in ("/health", "/metrics"):
                assert policy is RoutePolicy.PUBLIC
            else:
                assert policy is not RoutePolicy.PUBLIC, route.path

    def test_no_route_accepts_tenant_id_from_request(self):
        """Under the single-tenant credential, tenant selection never comes
        from query or body data."""
        import api.routes as routes_module

        for route in app.routes:
            if not isinstance(route, APIRoute):
                continue
            param_names = {p.name for p in route.dependant.query_params}
            param_names |= {p.name for p in route.dependant.body_params}
            assert "tenant_id" not in param_names, route.path

        src = inspect.getsource(routes_module)
        assert 'get("tenant_id"' not in src

    def test_pinned_tenant_is_the_configured_default(self, use_settings):
        use_settings(default_tenant_id="the-agency")
        assert pinned_tenant_id() == "the-agency"

    def test_enforcement_is_installed_app_wide(self):
        """The policy is enforced from one place: an app-level dependency."""
        dep_calls = [d.dependency for d in app.router.dependencies]
        assert security.enforce_route_security in dep_calls
