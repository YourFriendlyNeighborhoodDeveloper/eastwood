"""Telephony client selection: real Twilio, the load-test simulator, or none.

TELEPHONY_PROVIDER=simulated replaces Twilio with SimulatedTelephony — for
load and end-to-end testing only; production refuses to boot with it.
"""
from __future__ import annotations


def make_telephony_client(settings):
    provider = (settings.telephony_provider or "twilio").strip().lower()
    if provider == "simulated":
        from clients.simulated_telephony import SimulatedTelephony

        return SimulatedTelephony(settings)
    if settings.twilio_account_sid:
        from clients.twilio_sms import SmsClient

        return SmsClient()
    return None
