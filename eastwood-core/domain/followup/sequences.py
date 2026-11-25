"""
Follow-up sequence definitions.

Each sequence is a list of steps with a delay and a message template.
Templates use {first_name}, {agency}, {agent_name}, {quote_amount}, {carrier}
and similar, populated from the lead's context_data at fire time.

State-aware: the system knows the last call outcome and personalizes accordingly.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta


@dataclass
class SequenceStep:
    delay: timedelta
    channel: str        # sms | call
    template_key: str   # maps to SMS_TEMPLATES below


# ── SMS message templates ─────────────────────────────────────────────────────
# Every text names the agency ({agency}), never claims to be a specific person
# (the call may have been placed by the AI assistant), makes no deadline or
# "rate held" claims, and stays in the GSM-7 character set (no em dashes or
# curly quotes: one of those switches the whole text to UCS-2, which fits 70
# characters per segment instead of 160 and multiplies its cost). Dollar figures
# are filled only from a stored, verified quote (domain.followup.worker); when
# there is none, the *_noprice version goes instead. The worker passes every
# rendered text through the same policy check as Imogen's SMS conversations.

SMS_TEMPLATES: dict[str, str] = {
    # consent request (queued by routing for leads without express written consent)
    "consent_request": (
        "Hi {first_name}, this is {agency}. You recently asked for insurance information. "
        "Reply YES if we may call you about your options. Reply STOP to opt out. Msg&data rates may apply."
    ),
    # missed_call
    "missed_call_immediate": (
        "Hi {first_name}, this is {agency}. We just tried to call about the insurance information "
        "you requested. When is a good time, or would you rather text? Reply STOP to opt out."
    ),
    "missed_call_next_day": (
        "Hi {first_name}, {agency} here, following up on your insurance request. "
        "Would 5 minutes today or tomorrow work? Reply STOP to opt out."
    ),

    # voicemail_left
    "voicemail_sms": (
        "Hi {first_name}, {agency} here. We left you a voicemail about your coverage request. "
        "If texting is easier, just reply here. Reply STOP to opt out."
    ),

    # interested_busy
    "interested_busy_callback": (
        "Hi {first_name}, {agency} here. You mentioned {callback_window} works, so we will call then. "
        "Reply here if anything changes. Reply STOP to opt out."
    ),

    # quoted_not_closed — figures come from the quote persisted on the call,
    # never re-computed for the SMS; {calendly_link} books time with the agent.
    "quoted_followup_day1": (
        "Hi {first_name}, {agency} here, following up on the ${face_value} option we discussed: "
        "${quote_amount}/month ({quote_label}, {carrier}). The next step is a short call with {agent_name}. "
        "{booking} Reply STOP to opt out."
    ),
    "quoted_followup_day1_noprice": (
        "Hi {first_name}, {agency} here, following up on the coverage options we discussed. "
        "Exact prices come from a short call with {agent_name}. {booking} Reply STOP to opt out."
    ),
    "quoted_followup_day3": (
        "Hi {first_name}, {agency} here, checking back on the ${face_value} option at "
        "${quote_amount}/month ({quote_label}) and a short call with {agent_name}. {booking} "
        "Reply STOP to opt out."
    ),
    "quoted_followup_day3_noprice": (
        "Hi {first_name}, {agency} here, checking back on the coverage options we discussed and a "
        "short call with {agent_name}. {booking} Reply STOP to opt out."
    ),
    "quoted_objection_price": (
        "Hi {first_name}, {agency} here. Understood on the budget. A smaller coverage amount "
        "costs less. Want {agent_name} to show you what fits? Reply STOP to opt out."
    ),
    "quoted_objection_spouse": (
        "Hi {first_name}, {agency} here. Totally understand wanting to talk it over with your spouse. "
        "We can set up a call with both of you. Just let us know. Reply STOP to opt out."
    ),

    # no_contact_sequence
    "no_contact_day3": (
        "Hi {first_name}, {agency} here about your insurance request. If now is not a good time, "
        "when would work? Reply STOP to opt out."
    ),
    "no_contact_day7": (
        "Hi {first_name}, {agency} here with our last follow-up. We wanted to make sure you got "
        "the information you were looking for. Reply any time. Reply STOP to opt out."
    ),

    # application_not_completed
    "app_not_completed_day1": (
        "Hi {first_name}, {agency} here. Your {carrier} application is not finished yet. "
        "Want a call back to complete it? Reply STOP to opt out."
    ),
    "app_not_completed_day3": (
        "Hi {first_name}, {agency} here. Your {carrier} application is still not finished, so the "
        "coverage is not active yet. Reply here if you would like help. Reply STOP to opt out."
    ),

    # payment_failed
    "payment_rescue": (
        "Hi {first_name}, {agency} here. Your {carrier} policy payment did not go through, and the "
        "coverage can lapse if it is not fixed. Reply here and we will help. Reply STOP to opt out."
    ),
}

# Templates that state a premium; each has a "<key>_noprice" fallback.
PRICE_TEMPLATES = frozenset({"quoted_followup_day1", "quoted_followup_day3"})


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
    ctx = dict(ctx)
    if "booking" not in ctx:
        # With a booking link: offer it; without one (Calendly not configured):
        # never a dangling "at  or reply here".
        link = str(ctx.get("calendly_link") or "").strip()
        ctx["booking"] = f"Book a time at {link} or reply here." if link else "Reply here to pick a time."
    template = SMS_TEMPLATES.get(
        template_key, "Hi {first_name}, {agency} here, following up on your insurance request. Reply STOP to opt out.")
    return template.format_map(_SafeDict(ctx))


_PLACEHOLDER = re.compile(r"\{[a-z_]+\}")


def unfilled_placeholders(body: str) -> list[str]:
    """Placeholders left in a rendered text (missing context) — never sent."""
    return _PLACEHOLDER.findall(body)


class _SafeDict(dict):
    """Returns the key placeholder if a value is missing — never KeyError."""
    def __missing__(self, key: str) -> str:
        return f"{{{key}}}"
