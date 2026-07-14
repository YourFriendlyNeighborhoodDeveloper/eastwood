"""
Call-outcome notification.

After EVERY completed connected call — not only booked appointments — Ryan gets
one outcome SMS assembled from the ProspectProfile the voice agent captured.
Facts only: a field the prospect never gave reads "unknown", never invented.

The destination comes from settings (AGENT_NOTIFICATION_PHONE_E164); it is not
scattered through business logic. Sends are deduplicated per session, fail-soft,
and never block call processing. Calls that never connected (no-answer,
disconnected before telephony bound) publish no call_ended and therefore send
nothing — there was no call, and no profile is fabricated for one.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)

_UNKNOWN = "unknown"

# Twilio hard-caps concatenated SMS at 1600 chars. Above this we send the
# concise brief and point at the persisted canonical profile — no field is
# silently discarded, it lives in handoff_profiles.
_SMS_SAFE_LIMIT = 1500

# Agent/hangup outcomes → the canonical outcome vocabulary Ryan reads.
_OUTCOME_MAP = {
    "appointment_set": "appointment_booked",
    "appointment_booked": "appointment_booked",
    "sold": "appointment_booked",
    "transferred": "transfer_requested",
    "transfer": "transfer_requested",
    "transfer_requested": "transfer_requested",
    "quoted_not_closed": "quoted_not_closed",
    "callback": "callback",
    "not_interested": "not_interested",
    "dnc": "DNC",
    "hangup": "disconnected",
    "disconnected": "disconnected",
    "quote_failed": "quote_failed",
}


class SmsSender(Protocol):
    def send(self, to: str, body: str) -> Any: ...


def canonical_outcome(outcome: str | None) -> str:
    if not outcome:
        return "other"
    return _OUTCOME_MAP.get(outcome.strip().lower(), "other")


def _val(profile: dict, key: str, default: str = _UNKNOWN) -> str:
    v = profile.get(key)
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return default
    return str(v)


def _money(v) -> str:
    if v is None:
        return _UNKNOWN
    try:
        return f"${int(float(v)):,}"
    except (ValueError, TypeError):
        return str(v)


def _age_or_dob(profile: dict) -> str:
    age = profile.get("age")
    if age:
        return str(age)
    return _val(profile, "date_of_birth")


def _beneficiary(profile: dict) -> str:
    name = _val(profile, "beneficiary_name")
    rel = _val(profile, "beneficiary_relationship")
    if name != _UNKNOWN and rel != _UNKNOWN:
        return f"{name} ({rel})"
    if name != _UNKNOWN:
        return name
    return rel


def _existing_coverage(profile: dict) -> str:
    status = _val(profile, "existing_coverage")
    amount = profile.get("existing_coverage_amount")
    if status == "yes" and amount:
        return f"yes ({_money(amount)})"
    return status


def _health_block(profile: dict) -> str:
    return (
        f"Conditions: {_val(profile, 'health_summary')}; "
        f"Meds: {_val(profile, 'medications_summary')}; "
        f"Tobacco: {_val(profile, 'tobacco_status')}; "
        f"Ht/Wt: {_val(profile, 'height_weight')}"
    )


def _option_line(profile: dict, label: str) -> str:
    for opt in profile.get("quote_options") or []:
        if isinstance(opt, dict) and str(opt.get("label", "")).upper() == label:
            face = _money(opt.get("face_value"))
            premium = opt.get("monthly_premium")
            p = f"${premium}/mo" if premium is not None else _UNKNOWN
            return f"{face} / {p}"
    return "none"


def _quote_source(profile: dict) -> str:
    source = _val(profile, "quote_source", default="")
    if source:
        return source
    qt = _val(profile, "quote_type", default="")
    if qt == "carrier":
        return "carrier-backed"
    if qt == "estimated":
        return "estimate"
    return _UNKNOWN


def _objections(profile: dict) -> str:
    objections = profile.get("objections") or []
    if not objections:
        return "none"
    items = []
    for o in objections:
        if isinstance(o, dict):
            items.append(f"{o.get('objectionType', 'concern')}: {o.get('text', '')}".strip())
        else:
            items.append(str(o))
    return "; ".join(items)


def _open_questions(profile: dict) -> str:
    items = profile.get("unanswered_questions") or []
    return "; ".join(str(i) for i in items) if items else "none"


def _appointment_block(profile: dict) -> list[str]:
    lines = [f"Appointment: {_val(profile, 'appointment_time', default='none')}"]
    calendly = _val(profile, "calendly_status", default="not requested").replace("_", " ")
    lines.append(f"Calendly status: {calendly}")
    return lines


def format_call_outcome(profile: dict, outcome: str | None) -> str:
    """Render the outcome SMS. Every value is captured fact or 'unknown'."""
    lines = [
        "EASTWOOD CALL COMPLETE",
        "",
        f"Prospect: {_val(profile, 'full_name')}",
        f"Phone: {_val(profile, 'phone_e164')}",
        f"State: {_val(profile, 'state')}",
        f"Age/DOB: {_age_or_dob(profile)}",
        f"Rating sex: {_val(profile, 'rating_sex')}",
        f"Employment/income: {_val(profile, 'employment_status')}",
        "",
        f"Purpose: {_val(profile, 'coverage_purpose')}",
        f"Protecting: {_beneficiary(profile)}",
        f"Why now: {_val(profile, 'motivation_summary')}",
        f"Existing coverage: {_existing_coverage(profile)}",
        f"Coverage requested: {_money(profile.get('desired_coverage_amount'))}",
        f"Budget: {_money(profile.get('monthly_budget'))}/month",
        "",
        "Health:",
        _health_block(profile),
        "",
        "Quote:",
        f"Option C: {_option_line(profile, 'C')}",
        f"Option B: {_option_line(profile, 'B')}",
        f"Option A: {_option_line(profile, 'A')}",
        f"Selected: {_val(profile, 'selected_option', default='none')}",
        f"Quote source: {_quote_source(profile)}",
        f"Carrier: {profile.get('carrier_name') or 'TBD'}",
    ]
    if profile.get("quote_failure_reason"):
        lines.append(f"Quote failure: {profile['quote_failure_reason']}")
    lines += [
        "",
        f"Outcome: {canonical_outcome(outcome)}",
        "",
        *_appointment_block(profile),
        "",
        f"Objections: {_objections(profile)}",
        f"Open questions: {_open_questions(profile)}",
        "",
        f"Summary: {_val(profile, 'transcript_summary')}",
    ]
    return "\n".join(lines)


def format_call_outcome_concise(profile: dict, outcome: str | None) -> str:
    """Short first-SMS fallback when the full brief exceeds provider limits.

    Nothing is discarded — the canonical profile is persisted, and this points
    Ryan at it.
    """
    lead_id = _val(profile, "lead_id")
    return "\n".join([
        "EASTWOOD CALL COMPLETE",
        f"Prospect: {_val(profile, 'full_name')} {_val(profile, 'phone_e164')}",
        f"Outcome: {canonical_outcome(outcome)}",
        f"Selected: {_val(profile, 'selected_option', default='none')} "
        f"({_quote_source(profile)})",
        f"Appointment: {_val(profile, 'appointment_time', default='none')} "
        f"(Calendly: {_val(profile, 'calendly_status', default='not requested')})",
        f"Full profile saved — lead {lead_id}.",
    ])


class CallOutcomeNotificationService:
    """Sends the per-call outcome SMS to Ryan. Fail-soft, exactly once per session."""

    # Process-wide dedupe: one connected call yields one outcome SMS even if the
    # call_ended event is delivered more than once.
    _notified: set[str] = set()

    def __init__(self, sms: SmsSender | None, destination: str | None) -> None:
        self._sms = sms
        self._destination = destination

    def send_outcome(self, profile: dict, *, session_id: str | None, outcome: str | None) -> bool:
        """Return True iff an SMS was sent on this call."""
        key = session_id or f"{profile.get('lead_id')}:{outcome}"
        if key in CallOutcomeNotificationService._notified:
            logger.info("outcome SMS already sent for session %s — skipping duplicate", key)
            return False

        if not self._destination:
            logger.warning(
                "AGENT_NOTIFICATION_PHONE_E164 not set — call outcome for lead %s not texted; "
                "profile persisted", profile.get("lead_id"),
            )
            return False
        if self._sms is None:
            logger.warning("SMS client not configured — outcome SMS skipped for lead %s", profile.get("lead_id"))
            return False

        body = format_call_outcome(profile, outcome)
        if len(body) > _SMS_SAFE_LIMIT:
            body = format_call_outcome_concise(profile, outcome)

        try:
            self._sms.send(self._destination, body)
        except Exception:
            logger.exception("failed to send outcome SMS for lead %s", profile.get("lead_id"))
            return False

        CallOutcomeNotificationService._notified.add(key)
        logger.info("outcome SMS sent for session %s (lead %s)", key, profile.get("lead_id"))
        return True
