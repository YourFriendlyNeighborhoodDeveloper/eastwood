"""The controlled live SMS test refuses to start unless every guard holds."""
from __future__ import annotations

from types import SimpleNamespace

from scripts.live_sms_test import preflight

TO = "+13135550123"


def _s(**over):
    base = dict(telephony_provider="twilio", twilio_account_sid="AC1", twilio_auth_token="t", twilio_from_number="+1",
                public_base_url="https://eastwood.example", simulated_sms_prospects=False, outbound_enabled=True,
                default_tenant_id="live-test-1")
    base.update(over)
    return SimpleNamespace(**base)


def _check(s=None, **kw):
    args = dict(to=TO, env_recipient=TO, tenant_phones={TO}, other_active_campaigns=0)
    args.update(kw)
    return preflight(s or _s(), **args)


def test_all_guards_hold():
    assert _check() == []


def test_each_guard_refuses():
    assert _check(_s(telephony_provider="simulated"))
    assert _check(_s(twilio_auth_token=""))
    assert _check(_s(public_base_url="http://localhost:8080"))
    assert _check(_s(simulated_sms_prospects=True))
    assert _check(_s(outbound_enabled=False))
    assert _check(_s(default_tenant_id="default"))          # never the real book of leads
    assert _check(env_recipient="")                         # the recipient must be named twice
    assert _check(env_recipient="+13135550999")
    assert _check(to="3135550123")
    assert _check(tenant_phones={TO, "+13135550188"})        # any other lead in the tenant
    assert _check(other_active_campaigns=1)


def test_refusals_explain_themselves():
    problems = _check(_s(default_tenant_id="default"), tenant_phones={TO, "+13135550188"})
    assert any("live-test" in p for p in problems) and any("1 other lead" in p for p in problems)
