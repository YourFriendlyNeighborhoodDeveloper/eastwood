"""
Follow-up sequence definitions.

Each sequence is a list of steps with a delay and a message template.
Templates use {first_name}, {quote_amount}, {carrier}, {last_objection}
— populated from the lead's context_data at fire time.

State-aware: the system knows the last call outcome and personalizes accordingly.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta


@dataclass
class SequenceStep:
    delay: timedelta
    channel: str        # sms | call
    template_key: str   # maps to SMS_TEMPLATES below


# ── SMS message templates ─────────────────────────────────────────────────────

SMS_TEMPLATES: dict[str, str] = {
    # missed_call
    "missed_call_immediate": (
        "Hi {first_name}, this is {agent_name} from Eastwood Insurance — "
        "I just tried to reach you about your insurance inquiry. "
        "Is now a better time, or when works for you? Reply STOP to opt out."
    ),
    "missed_call_next_day": (
        "Hi {first_name}, still hoping to connect with you about protecting your family. "
        "Would you have 5 minutes today or tomorrow? Reply STOP to opt out."
    ),

    # voicemail_left
    "voicemail_sms": (
        "Hi {first_name}, I just left you a voicemail about your coverage options — "
        "wanted to follow up in case it's easier to text. Reply STOP to opt out."
    ),

    # interested_busy
    "interested_busy_callback": (
        "Hi {first_name}, you mentioned you'd have time {callback_window}. "
        "I'll give you a call then. Just reply here if anything changes. Reply STOP to opt out."
    ),

    # quoted_not_closed
    "quoted_followup_day1": (
        "Hi {first_name}, wanted to follow up on the {carrier} plan we discussed — "
        "${quote_amount}/month for ${face_value} in coverage. "
        "Still available if you'd like to move forward. Reply STOP to opt out."
    ),
    "quoted_followup_day3": (
        "Hi {first_name}, just checking back in. The {carrier} rate of ${quote_amount}/month "
        "is still available but rates can change. Happy to answer any questions. Reply STOP to opt out."
    ),
    "quoted_objection_price": (
        "Hi {first_name}, I hear you on the budget. I found an option closer to your range — "
        "want me to walk you through it quickly? Reply STOP to opt out."
    ),
    "quoted_objection_spouse": (
        "Hi {first_name}, totally understand wanting to talk it over with your spouse. "
        "I can set up a call with both of you if that helps — just let me know. Reply STOP to opt out."
    ),

    # no_contact_sequence
    "no_contact_day3": (
        "Hi {first_name}, I've been trying to reach you about your insurance inquiry. "
        "If now isn't a good time, when would work? Reply STOP to opt out."
    ),
    "no_contact_day7": (
        "Hi {first_name}, last follow-up from me — just want to make sure you got the "
        "information you were looking for. Happy to help whenever you're ready. Reply STOP to opt out."
    ),

    # application_not_completed
    "app_not_completed_day1": (
        "Hi {first_name}, your {carrier} application is almost done — "
        "just needs a few more minutes to finalize. Want me to call you back to complete it? Reply STOP to opt out."
    ),
    "app_not_completed_day3": (
        "Hi {first_name}, your coverage isn't active yet. Your {carrier} rate of "
        "${quote_amount}/month is still held — but that won't last. Reply STOP to opt out."
    ),

    # payment_failed
    "payment_rescue": (
        "Hi {first_name}, we had a payment issue with your {carrier} policy. "
        "Your coverage may lapse — please call us at {office_phone} to resolve. Reply STOP to opt out."
    ),
}


# ── Sequence definitions ──────────────────────────────────────────────────────

SEQUENCES: dict[str, list[SequenceStep]] = {
    "missed_call": [
        SequenceStep(delay=timedelta(seconds=30), channel="sms", template_key="missed_call_immediate"),
        SequenceStep(delay=timedelta(hours=24),   channel="call", template_key="missed_call_next_day"),
        SequenceStep(delay=timedelta(days=3),     channel="sms",  template_key="no_contact_day3"),
    ],
    "voicemail_left": [
        SequenceStep(delay=timedelta(minutes=5),  channel="sms",  template_key="voicemail_sms"),
        SequenceStep(delay=timedelta(hours=24),   channel="call", template_key="missed_call_next_day"),
    ],
    "interested_busy": [
        SequenceStep(delay=timedelta(minutes=2),  channel="sms",  template_key="interested_busy_callback"),
    ],
    "quoted_not_closed": [
        SequenceStep(delay=timedelta(hours=4),    channel="sms",  template_key="quoted_followup_day1"),
        SequenceStep(delay=timedelta(days=3),     channel="sms",  template_key="quoted_followup_day3"),
        SequenceStep(delay=timedelta(days=7),     channel="call", template_key="quoted_followup_day3"),
    ],
    "quoted_price_objection": [
        SequenceStep(delay=timedelta(hours=2),    channel="sms",  template_key="quoted_objection_price"),
        SequenceStep(delay=timedelta(days=2),     channel="call", template_key="quoted_objection_price"),
    ],
    "quoted_spouse_objection": [
        SequenceStep(delay=timedelta(hours=1),    channel="sms",  template_key="quoted_objection_spouse"),
        SequenceStep(delay=timedelta(days=2),     channel="call", template_key="quoted_objection_spouse"),
    ],
    "no_contact": [
        SequenceStep(delay=timedelta(days=3),     channel="sms",  template_key="no_contact_day3"),
        SequenceStep(delay=timedelta(days=5),     channel="call", template_key="no_contact_day3"),
        SequenceStep(delay=timedelta(days=7),     channel="sms",  template_key="no_contact_day7"),
    ],
    "application_not_completed": [
        SequenceStep(delay=timedelta(hours=2),    channel="sms",  template_key="app_not_completed_day1"),
        SequenceStep(delay=timedelta(hours=24),   channel="call", template_key="app_not_completed_day1"),
        SequenceStep(delay=timedelta(days=3),     channel="sms",  template_key="app_not_completed_day3"),
    ],
    "payment_failed": [
        SequenceStep(delay=timedelta(hours=1),    channel="sms",  template_key="payment_rescue"),
        SequenceStep(delay=timedelta(hours=24),   channel="call", template_key="payment_rescue"),
    ],
}


def render_template(template_key: str, ctx: dict) -> str:
    template = SMS_TEMPLATES.get(template_key, "Hi {first_name}, following up on your insurance inquiry.")
    return template.format_map(_SafeDict(ctx))


class _SafeDict(dict):
    """Returns the key placeholder if a value is missing — never KeyError."""
    def __missing__(self, key: str) -> str:
        return f"{{{key}}}"
