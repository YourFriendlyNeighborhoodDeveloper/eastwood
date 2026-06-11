from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import List, Optional
from ...booking.slots import generate_slots

class SlotsResponse(BaseModel):
    token: str
    tz: str
    slots: List[str]

class BookRequest(BaseModel):
    token: str
    start_at: str = Field(..., description="ISO 8601 timestamp")

class BookResponse(BaseModel):
    booked: bool
    appointment_id: str
    start_at: str
    tz: str

def build_router(ctx) -> APIRouter:
    r = APIRouter(prefix="/api/booking", tags=["booking"])

    @r.get("/slots", response_model=SlotsResponse)
    def slots(token: str, days: int = 7):
        tok = ctx.booking.get(token)
        if not tok:
            raise HTTPException(status_code=404, detail="token_not_found")
        now = datetime.now(timezone.utc)
        if tok["expires_at"] <= now:
            raise HTTPException(status_code=410, detail="token_expired")
        tz = str(ctx.settings.work_hours.get("tz") or "America/Detroit")
        start = now
        end = now + (timezone.utc.utcoffset(now) or (end := None))
        existing = ctx.appointments.list_booked_between(start, now + timedelta_days(days))
        slot_dts = generate_slots(
            now_utc=now,
            days=days,
            work_hours=ctx.settings.work_hours,
            booked_utc=[a["start_at"] for a in existing],
        )
        out = [dt.astimezone(ZoneInfo(tz)).isoformat() for dt in slot_dts]
        return SlotsResponse(token=token, tz=tz, slots=out)

    @r.post("/book", response_model=BookResponse)
    def book(req: BookRequest):
        tok = ctx.booking.get(req.token)
        if not tok:
            raise HTTPException(status_code=404, detail="token_not_found")
        now = datetime.now(timezone.utc)
        if tok["expires_at"] <= now:
            raise HTTPException(status_code=410, detail="token_expired")

        lead_id = tok["lead_id"]
        if not ctx.consent.exists_for_lead(lead_id):
            raise HTTPException(status_code=409, detail="missing_consent")

        wh = ctx.settings.work_hours
        tz = str(wh.get("tz") or "America/Detroit")
        try:
            start_local = datetime.fromisoformat(req.start_at.replace("Z", "+00:00"))
        except Exception:
            raise HTTPException(status_code=400, detail="bad_start_at")

        if start_local.tzinfo is None:
            start_local = start_local.replace(tzinfo=ZoneInfo(tz))
        start_utc = start_local.astimezone(timezone.utc)

        if start_utc <= now:
            raise HTTPException(status_code=400, detail="slot_in_past")

        if not ctx.appointments.is_slot_available(start_utc):
            raise HTTPException(status_code=409, detail="slot_taken")

        appt_id = ctx.appointments.create(lead_id=lead_id, start_at=start_utc, tz=tz)

        lead = ctx.leads.get(lead_id) or {}
        to_lead = str(lead.get("phone_e164") or "")
        to_agent = str(ctx.settings.agent_notify_number or "")
        booking_link = f"{ctx.settings.booking_public_base_url}/?token={req.token}#book"

        lead_msg = f"Booked. {start_utc.astimezone(ZoneInfo(tz)).strftime('%a %b %d %I:%M %p')} ({tz}). Link: {booking_link} Reply STOP to opt out."
        agent_msg = f"BOOKED {lead.get('product','').upper()} {lead.get('state','')} {lead.get('zip','')} {start_utc.astimezone(ZoneInfo(tz)).strftime('%a %b %d %I:%M %p')} {tz} lead_id={lead_id}"

        ctx.sms.send(to=to_lead, body=lead_msg, lead_id=lead_id, kind="confirm")
        ctx.sms.send(to=to_agent, body=agent_msg, lead_id=lead_id, kind="agent_confirm")
        ctx.appointments.mark_confirm_sent(appt_id)

        return BookResponse(booked=True, appointment_id=appt_id, start_at=start_utc.isoformat(), tz=tz)

    return r

def timedelta_days(days: int):
    from datetime import timedelta
    return timedelta(days=int(days))