"""
Appointment handoff notification.

When an appointment books, the licensed agent gets an immediate SMS brief built
from the ProspectProfile the voice agent assembled. This is the alert; the full
profile is persisted separately (handoff_profiles) as the canonical record.

Kept out of the booking route and out of inline Twilio code so the send is:
  - only attempted after the appointment write has succeeded, and
  - idempotent — one appointment produces one brief, never two.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)

_UNKNOWN = "unknown"


class SmsSender(Protocol):
    def send(self, to: str, body: str) -> Any: ...


def _val(profile: dict, key: str, default: str = _UNKNOWN) -> str:
    v = profile.get(key)
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return default
    return str(v)


def _money(profile: dict, key: str) -> str:
    v = profile.get(key)
    if v is None:
        return _UNKNOWN
    try:
        return f"${int(float(v)):,}"
    except (ValueError, TypeError):
        return str(v)


def _existing_coverage_line(profile: dict) -> str:
    status = _val(profile, "existing_coverage")
    amount = profile.get("existing_coverage_amount")
    if status == "yes" and amount:
        return f"yes ({_money(profile, 'existing_coverage_amount')})"
    return status


def _quote_line(profile: dict) -> str:
    face = _money(profile, "quote_amount")
    premium = profile.get("quoted_monthly_premium")
    if premium is None:
        return "no quote discussed"
    carrier = profile.get("carrier_name")
    tail = carrier if carrier else "estimate, carrier TBD"
    return f"{face} at approximately ${premium}/month\n{tail}"


def _health_summary(profile: dict) -> str:
    conditions = _val(profile, "health_summary")
    meds = _val(profile, "medications_summary")
    tobacco = _val(profile, "tobacco_status")
    return (
        f"  Conditions: {conditions}\n"
        f"  Medications: {meds}\n"
        f"  Tobacco: {tobacco}"
    )


def _objections_line(profile: dict) -> str:
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


def _list_line(profile: dict, key: str) -> str:
    items = profile.get(key) or []
    if not items:
        return "none"
    return "; ".join(str(i) for i in items)


def format_agent_brief(profile: dict) -> str:
    """Render the handoff SMS. Missing fields read as 'unknown', never invented."""
    beneficiary = _val(profile, "beneficiary_name")
    if beneficiary == _UNKNOWN:
        beneficiary = _val(profile, "beneficiary_relationship")
    else:
        rel = _val(profile, "beneficiary_relationship")
        if rel != _UNKNOWN:
            beneficiary = f"{beneficiary} ({rel})"

    return "\n".join([
        "NEW EASTWOOD APPOINTMENT",
        "",
        f"Prospect: {_val(profile, 'full_name')}",
        f"Phone: {_val(profile, 'phone_e164')}",
        f"Appointment: {_val(profile, 'appointment_time')}",
        "",
        f"Protecting: {beneficiary}",
        f"Purpose: {_val(profile, 'coverage_purpose')}",
        f"Coverage wanted: {_money(profile, 'desired_coverage_amount')}",
        f"Budget: {_money(profile, 'monthly_budget')}/month",
        f"Existing coverage: {_existing_coverage_line(profile)}",
        "",
        "Health:",
        _health_summary(profile),
        "",
        "Quote discussed:",
        _quote_line(profile),
        "",
        "Motivation:",
        _val(profile, "motivation_summary"),
        "",
        "Objections / concerns:",
        _objections_line(profile),
        "",
        "Open questions:",
        _list_line(profile, "unanswered_questions"),
        "",
        "Call summary:",
        _val(profile, "transcript_summary"),
    ])


class AppointmentNotificationService:
    """Sends the agent brief. Fail-soft: a missing destination or an SMS error
    never breaks the prospect-facing booking."""

    # Process-wide dedupe: one appointment yields one brief even if the booking
    # event is delivered twice.
    _notified: set[str] = set()

    def __init__(self, sms: SmsSender | None, destination: str | None) -> None:
        self._sms = sms
        self._destination = destination

    def send_agent_brief(self, profile: dict, *, appointment_id: str | None = None) -> bool:
        """Return True iff a brief was sent on this call."""
        key = appointment_id or f"{profile.get('lead_id')}:{profile.get('appointment_time')}"
        if key in AppointmentNotificationService._notified:
            logger.info("agent brief already sent for %s — skipping duplicate", key)
            return False

        if not self._destination:
            logger.warning(
                "AGENT_NOTIFICATION_PHONE_E164 not set — appointment booked but no agent "
                "brief sent; profile persisted for lead %s", profile.get("lead_id"),
            )
            return False
        if self._sms is None:
            logger.warning("SMS client not configured — agent brief skipped for lead %s", profile.get("lead_id"))
            return False

        try:
            self._sms.send(self._destination, format_agent_brief(profile))
        except Exception:
            logger.exception("failed to send agent brief for lead %s", profile.get("lead_id"))
            return False

        AppointmentNotificationService._notified.add(key)
        logger.info("agent brief sent for appointment %s (lead %s)", key, profile.get("lead_id"))
        return True
