"""Booking from an SMS conversation, and the profile the agent receives.

Reservation goes through the agent's own calendar (domain.scheduling.local):
advisory lock + partial unique index, so a slot is never double-booked.
Cancelling or rescheduling releases the slot (available = true takes it out
of the unique index) and keeps the appointment row with its outcome.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from config.settings import get_settings
from db.models import Appointment, BookingSlot, HandoffProfile, QuoteRecord, SmsMessage

from . import playbook


async def _quotes(session, conv) -> list[QuoteRecord]:
    ids = [o["quote_id"] for o in (conv.slots or {}).get("options", [])]
    if not ids:
        return []
    rows = (await session.execute(select(QuoteRecord).where(QuoteRecord.id.in_(ids)))).scalars()
    by_id = {str(r.id): r for r in rows}
    return [by_id[i] for i in ids if i in by_id]


async def build_profile(session, conv, lead, start: datetime | None) -> dict:
    s = get_settings()
    slots = conv.slots or {}
    quotes = await _quotes(session, conv)
    options = [{
        "label": str(i), "quote_id": str(q.id), "quote_ref": q.quote_ref, "carrier": q.carrier,
        "plan_name": (q.quote_data or {}).get("plan_name"), "face_value": int((q.face_value or 0) / 100),
        "monthly_premium": str(q.monthly_premium), "tier": (q.quote_data or {}).get("tier"),
        "rate_sheet_effective": (q.quote_data or {}).get("effective_date"),
        "source": q.source, "preliminary": True,
    } for i, q in enumerate(sorted(quotes, key=lambda q: q.face_value or 0), start=1)]
    selected = next((o for o in options if o["quote_id"] == slots.get("selected_quote_id")), None)
    health = slots.get("health") or {}
    questions = {q["key"]: q["text"] for q in slots.get("tier_questions") or []}
    health_summary = "; ".join(f"{questions.get(k, k)} {'YES' if v else 'no'}" for k, v in health.items()) \
        or "no health questions asked by text"
    msgs = list((await session.execute(
        select(SmsMessage).where(SmsMessage.conversation_id == conv.id).order_by(SmsMessage.created_at.desc()).limit(40)
    )).scalars())[::-1]
    transcript = "\n".join(f"{'Prospect' if m.direction == 'in' else 'Imogen'}: {m.body}" for m in msgs)
    name = " ".join(x for x in (lead.first_name, lead.last_name) if x) or "unknown"

    if selected:
        next_action = (f"Confirm tobacco status and health answers, then take the {selected['carrier']} "
                       f"{selected['plan_name']} application for ${selected['face_value']:,} "
                       f"(preliminary ${selected['monthly_premium']}/mo, quote {selected['quote_ref']}).")
    elif options:
        next_action = "Walk through the quoted options; the prospect has not picked one yet."
    elif slots.get("unavailable"):
        next_action = "No exact rate could be quoted by text (" + "; ".join(slots["unavailable"])[:200] + "). Run a quote on the call."
    else:
        next_action = "Complete needs analysis and quote on the call."
    if "spouse" in (slots.get("objections") or []):
        next_action += " Prospect wants their spouse on the call."

    when_local = playbook.when(start, lead.state) if start else None
    from zoneinfo import ZoneInfo

    when_agent = start.astimezone(ZoneInfo(s.calendly_default_timezone)).strftime("%a %b %d %I:%M %p") if start else None
    return {
        "lead_id": str(lead.id), "conversation_id": str(conv.id), "source": "sms",
        "full_name": name, "phone_e164": lead.phone_e164, "email": lead.email, "state": lead.state,
        "age": slots.get("age"), "sex": slots.get("sex"),
        "appointment_time": f"{when_local} (agent time {when_agent} {s.calendly_default_timezone})" if start else None,
        "calendly_status": "not_requested",
        "coverage_purpose": slots.get("purpose") or (lead.product_line or "final_expense").replace("_", " "),
        "desired_coverage_amount": slots.get("face"),
        "monthly_budget": slots.get("budget") or None,
        "tobacco_status": None if slots.get("tobacco") is None else ("tobacco" if slots["tobacco"] else "non-tobacco"),
        "health_summary": health_summary, "rate_tier": slots.get("tier"),
        "quote_options": options,
        "selected_option": selected["label"] if selected else None,
        "quote_amount": selected["face_value"] if selected else None,
        "quoted_monthly_premium": selected["monthly_premium"] if selected else None,
        "carrier_name": selected["carrier"] if selected else None,
        "quote_ref": selected["quote_ref"] if selected else None,
        "quote_preliminary": True,
        "objections": list(dict.fromkeys(slots.get("objections") or [])),
        "unanswered_questions": slots.get("questions") or [],
        "qualification_summary": (
            f"Age {slots.get('age', '?')}, {'tobacco' if slots.get('tobacco') else 'non-tobacco' if slots.get('tobacco') is False else 'tobacco unknown'}, "
            f"wants ${int(slots['face']):,}" if slots.get("face") else "coverage amount not given"),
        "motivation_summary": slots.get("purpose") or "not stated",
        "transcript_summary": transcript,
        "recommended_next_action": next_action,
    }


async def book(session, conv, lead, start: datetime, calendar, *, now: datetime | None = None) -> Appointment:
    """Reserve the slot and write appointment + handoff profile (caller commits)."""
    slot = await calendar.reserve(session, conv.tenant_id, start, now=now)
    appt = Appointment(lead_id=lead.id, slot_id=slot.id, tenant_id=conv.tenant_id, status="scheduled",
                       timezone=str(playbook.local_tz(lead.state).key), calendly_status="not_requested",
                       conversation_id=conv.id, reminders_sent=[])
    session.add(appt)
    await session.flush()
    profile = await build_profile(session, conv, lead, slot.starts_at)
    session.add(HandoffProfile(lead_id=lead.id, appointment_id=appt.id, tenant_id=conv.tenant_id,
                               session_id=f"sms:{conv.id}", profile=profile))
    await session.flush()
    return appt


async def cancel(session, appointment_id, *, outcome: str = "cancelled") -> Appointment | None:
    appt = await session.get(Appointment, appointment_id)
    if appt is None or appt.status not in ("scheduled", "requested"):
        return None
    appt.status = "cancelled"
    appt.outcome = outcome
    appt.cancelled_at = datetime.now(timezone.utc)
    slot = await session.get(BookingSlot, appt.slot_id)
    if slot is not None:
        slot.available = True  # frees the time (and the unique index) for someone else
    await session.flush()
    return appt
