"""
Regression: agent_notification_phone_e164 must default to "".

AppointmentNotificationService's fail-soft warning path (see
test_appointment_handoff.py::test_missing_destination_does_not_send_and_does_not_raise)
only fires when the destination is falsy. A non-empty default here is
silently unrecoverable: every booking "succeeds" and sends a real SMS to
whatever this value is instead of to the operator, with no warning anywhere.
This previously happened — the default was a hardcoded, non-operator number.
"""
from __future__ import annotations

from config.settings import Settings


def test_agent_notification_phone_defaults_to_empty(monkeypatch):
    # Isolate from any AGENT_NOTIFICATION_PHONE_E164 set in the environment
    # or a local .env — this test asserts the *code* default, not the
    # deployed configuration.
    monkeypatch.delenv("AGENT_NOTIFICATION_PHONE_E164", raising=False)
    settings = Settings(_env_file=None)
    assert settings.agent_notification_phone_e164 == "", (
        "a non-empty default silently sends the appointment brief to the "
        "wrong number instead of skipping with a warning"
    )
