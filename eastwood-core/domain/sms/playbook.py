"""What Imogen says by text — reviewed wording, chosen by code, never generated.

Every outbound text is composed from these templates by domain.sms.engine.
The language model (if enabled) only helps UNDERSTAND replies; it never writes
a message, so it cannot invent a price, a promise or a policy detail. Quotes are
inserted from stored quote records only. All wording is plain GSM-7 (no curly
quotes, dashes or emoji) so a text costs as few segments as possible.

Each step has several phrasings; the engine rotates them per conversation so a
prospect does not receive the same sentence twice.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo

PRODUCT_PHRASE = {
    "final_expense": "final expense (burial) insurance",
    "life": "life insurance",
}
PRODUCT_SHORT = {"final_expense": "final expense", "life": "life insurance"}

# Texts sent to every lead (the opener and the no-reply nudges) are most of the
# SMS bill. For these the engine uses the variant that fits ONE segment (160
# GSM-7 characters) whenever one does; see SmsEngine._say.
ONE_SEGMENT_KEYS = frozenset({"opener", "followup_1", "followup_2"})

T: dict[str, list[str]] = {
    "opener": [
        "Hi {first}, this is Imogen, the AI assistant at {agency}, following up on the {product} info you asked about. "
        "Want me to text you a few quick rate options? Reply YES, or STOP to opt out.",
        "Hi {first}, Imogen here, an AI assistant with {agency}. You asked about {product}. I can text you exact "
        "rates in a couple of minutes. Interested? Reply YES, or STOP to opt out.",
        "Hi {first}, Imogen here, an AI assistant at {agency}. You asked about {product_short}. Want exact rates by "
        "text? Reply YES, or STOP to opt out.",
        "Hi {first}, it's Imogen, AI assistant at {agency}. Want {product_short} rates by text? Reply YES, or STOP "
        "to opt out.",
    ],
    "followup_1": [
        "Hi {first}, just checking in from {agency}. Would you still like a few quick {product} rate options? Reply YES or STOP to opt out.",
        "{first}, Imogen from {agency} again. Happy to text you {product} rates whenever you're ready. Just reply YES. Reply STOP to opt out.",
        "Hi {first}, {agency} again. Still want {product_short} rates by text? Reply YES, or STOP to opt out.",
    ],
    "followup_2": [
        "Last note from me, {first}: if you want {product} rates later, reply YES anytime. Otherwise I won't text again. Reply STOP to opt out.",
    ],
    "stall_1": [
        "Hi {first}, still want those rates? {pending}",
        "{first}, picking up where we left off. {pending}",
    ],
    "stall_2": [
        "Hi {first}, I'll pause here. If you'd like your rates later, just reply and we'll pick up where we left off.",
    ],
    "welcome_back": [
        "Welcome back, {first}! Picking up where we left off. {pending}",
        "Good to hear from you, {first}. {pending}",
    ],
    "start_qualifying": [
        "Great! A few quick questions so the rates are exact. {pending}",
        "Perfect. Just a few questions and I'll have real rates for you. {pending}",
    ],
    "ack": ["Got it.", "Thanks.", "Perfect.", "Okay, noted."],
    # Questions for missing facts (pending prompts)
    "ask_face": [
        "How much coverage would you like? For example 10,000 or 15,000.",
        "What coverage amount are you thinking of? Many people choose between 10,000 and 20,000.",
    ],
    "ask_age": ["How old are you?", "What is your current age?"],
    "ask_tobacco": [
        "Have you used tobacco or nicotine in the last 12 months? (yes or no)",
        "Any tobacco or nicotine use in the past 12 months? (yes or no)",
    ],
    "ask_sex": [
        "Rates are set by the sex on the application. Should I price it as male or female?",
    ],
    "ask_budget": [
        "Is there a monthly amount you'd like to stay under? If not, just say no.",
    ],
    "ask_health": ["{question} (yes or no)"],
    "clarify": [
        "Sorry, I didn't catch that. {pending}",
        "Just to make sure I get it right: {pending}",
    ],
    # Quotes
    "quote_intro": [
        "Here are your preliminary rates from {carrier}, {plan}, paid monthly:",
        "Your preliminary monthly rates from {carrier} ({plan}):",
    ],
    "quote_outro": [
        "Final price is set by the application. Which works best? Reply {choices}.",
        "The final price depends on the application. Reply {choices} to pick one.",
    ],
    "quote_outro_single": [
        "Final price is set by the application. Want to go over it with {agent}? Reply YES.",
    ],
    "quote_unavailable": [
        "I can't give you an exact rate by text for your situation, but {agent} can run exact numbers with you. Want to set up a short call?",
    ],
    "lower_option": [
        "Understood. The lowest option I have is {option}. Would that work, or should {agent} look at other plans with you?",
    ],
    # Booking
    "offer_times": [
        "Great choice. {agent} can call you at one of these times:\n{times}\nReply {choices}, or tell me a day that works better.",
        "{agent} can go over it with you by phone. Pick a time:\n{times}\nReply {choices}, or suggest another day.",
    ],
    "offer_times_again": [
        "No problem. Here are other times:\n{times}\nReply {choices}.",
    ],
    "calendar_full": [
        "{agent}'s calendar is full right now. I'll text you as soon as a time opens up.",
    ],
    "booked": [
        "You're booked! {agent} will call you {when} at this number. Reply R to reschedule or C to cancel.",
    ],
    "slot_taken": [
        "That time was just taken. Here are the next open times:\n{times}\nReply {choices}.",
    ],
    "reminder": [
        "Reminder: {agent} will call you {when} at this number about your {product} options. Reply R to reschedule or C to cancel.",
    ],
    "cancelled": [
        "Your call with {agent} is cancelled. If you'd like a new time later, just reply here.",
    ],
    "after_booked": [
        "You're all set for {when}. Reply R to reschedule or C to cancel.",
    ],
    # Closing / handling
    "declined": [
        "Understood, {first}. I won't text you about this again. Take care.",
    ],
    "wrong_number": [
        "Sorry about that! I'll remove this number. Have a good day.",
    ],
    "snooze": [
        "No problem, {first}. I'll check back in a few days.",
    ],
    "voice_escalation": [
        "Sure. Our AI assistant will give you a call at this number in a few minutes.",
    ],
    "confirm_opt_in": [
        "Would you like me to text you a few quick rate options? Reply YES, or STOP to opt out.",
    ],
    "handoff_call": [
        "I've let {agent} know you'd like a call. They'll reach you as soon as they can.",
    ],
    # Objections
    "objection_spouse": [
        "That makes sense. Would you like a time when you and your spouse can both talk with {agent}?",
    ],
    "objection_have_coverage": [
        "Good to hear. Some people like to double-check their coverage is enough. {agent} can review it with you. Want a time?",
    ],
    "objection_trust": [
        "Fair question. I'm an AI assistant for {agency}, and {agent} is a licensed agent. You never have to share anything you're not comfortable with, and you can reply STOP anytime.",
    ],
    # FAQ (approved answers)
    "faq_who": [
        "I'm Imogen, an AI assistant for {agency}. I set up exact quotes and calls with {agent}, a licensed agent.",
    ],
    "faq_how_got_number": [
        "Our records show you asked about {product} options. If you'd rather not hear from us, reply STOP.",
    ],
    "faq_is_ai": [
        "Yes, I'm an AI assistant. {agent} is a real, licensed agent who handles your call.",
    ],
    "faq_what_covers": [
        "Final expense is whole life coverage meant for funeral costs and final bills. It pays your beneficiary. {agent} can walk you through the details.",
    ],
    "faq_exam": [
        "Many of these plans have no medical exam, just health questions. Some plans have a waiting period depending on health. {agent} will cover the exact terms.",
    ],
    "faq_cost": [
        "I can get you exact rates from the carrier's rate sheet in a minute.",
    ],
    "faq_unknown": [
        "Good question. I'll note it for {agent} to answer on your call.",
    ],
}


def pick(key: str, conversation_id: str, step: int | str, avoid: str | None = None) -> tuple[str, str]:
    """Deterministic variant per conversation and step; never repeats the
    previous variant of the same template if another exists."""
    variants = T[key]
    h = int(hashlib.sha256(f"{conversation_id}:{key}:{step}".encode()).hexdigest(), 16)
    idx = h % len(variants)
    variant_id = f"{key}#{idx}"
    if variant_id == avoid and len(variants) > 1:
        idx = (idx + 1) % len(variants)
        variant_id = f"{key}#{idx}"
    return variants[idx], variant_id


_TZ_ABBR = {"America/New_York": "ET", "America/Detroit": "ET", "America/Indiana/Indianapolis": "ET",
            "America/Chicago": "CT", "America/Denver": "MT", "America/Phoenix": "MT",
            "America/Los_Angeles": "PT", "America/Anchorage": "AKT", "Pacific/Honolulu": "HT"}


def local_tz(state: str | None) -> ZoneInfo:
    from domain.tcpa import _STATE_TZ  # noqa: PLC2701 - shared state->zone table

    return ZoneInfo(_STATE_TZ.get((state or "").upper(), "America/New_York"))


def when(start: datetime, state: str | None) -> str:
    """'Tue Dec 1 at 5:00 PM ET' in the prospect's own timezone."""
    tz = local_tz(state)
    local = start.astimezone(tz)
    abbr = _TZ_ABBR.get(str(tz.key), "")
    hour = local.strftime("%I:%M %p").lstrip("0")
    return f"{local.strftime('%a %b')} {local.day} at {hour} {abbr}".strip()


def choices(n: int) -> str:
    """How to answer a numbered list of n items: "1", "1 or 2", "1, 2 or 3"."""
    if n <= 1:
        return "1"
    nums = [str(i) for i in range(1, n + 1)]
    return ", ".join(nums[:-1]) + " or " + nums[-1]


def slot_lines(starts: list[datetime], state: str | None) -> str:
    return "\n".join(f"{i}) {when(s, state)}" for i, s in enumerate(starts, start=1))


def money(amount) -> str:
    return f"${amount:,.2f}"


def face(amount: int) -> str:
    return f"${amount:,}"
