from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.security import pinned_tenant_id
from config.settings import get_settings
from db.session import get_session
from kafka_io.kafka_client import publish
from repos.repos import BatchRepo, BookingRepo, CallRepo
from domain.normalization import normalize_lead_payload

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/leads/ingest", status_code=status.HTTP_202_ACCEPTED)
async def ingest_lead(request: Request, session: AsyncSession = Depends(get_session)):
    payload: dict[str, Any] = await request.json()
    settings = get_settings()

    normalized = normalize_lead_payload(payload)
    if not normalized.get("phone_e164"):
        raise HTTPException(status_code=422, detail="invalid or missing phone number")

    await publish(
        settings.topic_leads_raw,
        {**normalized, "source": payload.get("source", "api")},
        key=normalized.get("phone_e164"),
    )

    return {"status": "accepted"}


@router.post("/api/webhooks/facebook")
async def facebook_webhook(request: Request):
    settings = get_settings()

    if request.method == "GET":
        params = dict(request.query_params)
        if params.get("hub.verify_token") == settings.facebook_verify_token:
            return JSONResponse(content=int(params.get("hub.challenge", 0)))
        raise HTTPException(status_code=403, detail="invalid verify token")

    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")

    # Fail closed: without the app secret no signature can be verified, and an
    # unsigned POST must never become a lead. (This route is classified
    # PROVIDER_SIGNED_WEBHOOK in api.security — this check IS its protection.)
    if not settings.facebook_app_secret:
        logger.warning(
            "facebook webhook denied: route=%s result=denied reason=app_secret_unconfigured",
            request.url.path,
        )
        raise HTTPException(status_code=403, detail="forbidden")

    expected = "sha256=" + hmac.new(
        settings.facebook_app_secret.encode(),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=403, detail="invalid signature")

    import orjson
    data = orjson.loads(raw_body)

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") == "leadgen":
                value = change.get("value", {})
                await publish(
                    settings.topic_leads_raw,
                    {**value, "source": "facebook_leadgen"},
                    key=str(value.get("leadgen_id", "")),
                )

    return {"status": "ok"}


@router.get("/api/booking")
async def get_booking_slots(
    session: AsyncSession = Depends(get_session),
):
    from datetime import datetime, timezone
    t_id = pinned_tenant_id()
    repo = BookingRepo(session)
    slots = await repo.get_available_slots(t_id, after=datetime.now(timezone.utc))
    return {"slots": [{"id": str(s.id), "starts_at": s.starts_at.isoformat(), "ends_at": s.ends_at.isoformat()} for s in slots]}


@router.api_route("/twiml/call/{session_id}", methods=["GET", "POST"], response_class=None)
async def twiml_call_webhook(session_id: str):
    """Twilio fetches this when the outbound call connects. Returns TwiML that
    streams audio to the ai-agent WebSocket via nginx's /agent/ proxy."""
    from starlette.responses import Response as StarletteResponse
    settings = get_settings()
    public_base = settings.public_base_url.rstrip("/")
    ws_base = public_base.replace("https://", "wss://").replace("http://", "ws://")
    ws_url = f"{ws_base}/agent/sessions/{session_id}/media"
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Connect><Stream url="{ws_url}" /></Connect>'
        "</Response>"
    )
    return StarletteResponse(content=twiml, media_type="application/xml")


@router.post("/api/triage")
async def triage_single(request: Request, session: AsyncSession = Depends(get_session)):
    """Score a single lead inline — no Kafka, returns result immediately."""
    from domain.triage import triage_from_lead_dict
    from repos.repos import RulesetRepo

    body: dict = await request.json()
    ruleset_repo = RulesetRepo(session)
    active = await ruleset_repo.get_active()
    ruleset_payload = active.payload if active else None

    result = triage_from_lead_dict(
        lead=body.get("lead", body),
        health_data=body.get("health_data", {}),
        ruleset=ruleset_payload,
    )
    return result.model_dump()


@router.post("/api/triage/batch")
async def triage_batch(request: Request, session: AsyncSession = Depends(get_session)):
    """Score a list of leads and return them ranked by priority descending."""
    from domain.triage import triage_from_lead_dict
    from repos.repos import RulesetRepo

    body: dict = await request.json()
    leads: list[dict] = body.get("leads", [])
    if not leads:
        raise HTTPException(status_code=422, detail="leads list is required")

    ruleset_repo = RulesetRepo(session)
    active = await ruleset_repo.get_active()
    ruleset_payload = active.payload if active else None

    results = []
    for item in leads:
        r = triage_from_lead_dict(
            lead=item.get("lead", item),
            health_data=item.get("health_data", {}),
            ruleset=ruleset_payload,
        )
        results.append(r.model_dump())

    results.sort(key=lambda r: r["priority"], reverse=True)
    return {"count": len(results), "results": results}


@router.get("/api/ruleset")
async def get_ruleset(session: AsyncSession = Depends(get_session)):
    """Return the currently active ruleset version and its thresholds."""
    from repos.repos import RulesetRepo

    ruleset_repo = RulesetRepo(session)
    active = await ruleset_repo.get_active()
    if not active:
        raise HTTPException(status_code=404, detail="no active ruleset found")

    p = active.payload
    return {
        "version": active.version,
        "effective_date": active.effective_date.isoformat() if active.effective_date else None,
        "thresholds": p.get("thresholds", {}),
        "max_issue_age": p.get("max_issue_age"),
        "min_issue_age": p.get("min_issue_age"),
        "payload": p,
    }


# ── Lead batch management ─────────────────────────────────────────────────────

@router.post("/api/batches", status_code=201)
async def create_batch(request: Request, session: AsyncSession = Depends(get_session)):
    """Register a new lead purchase so every lead can be attributed to cost."""
    body: dict = await request.json()
    repo = BatchRepo(session)
    import secrets
    batch_id = body.get("batch_id") or f"batch_{secrets.token_hex(6)}"
    batch = await repo.create(
        batch_id=batch_id,
        tenant_id=pinned_tenant_id(),
        vendor_name=body["vendor_name"],
        source_tag=body["source_tag"],
        cost_cents=int(body.get("cost_cents", 0)),
        notes=body.get("notes"),
    )
    return {"batch_id": batch.id, "vendor_name": batch.vendor_name, "cost_cents": batch.cost_cents}


@router.get("/api/batches")
async def list_batches(session: AsyncSession = Depends(get_session)):
    repo = BatchRepo(session)
    batches = await repo.list_by_tenant(pinned_tenant_id())
    return {"batches": [
        {"batch_id": b.id, "vendor_name": b.vendor_name, "source_tag": b.source_tag,
         "cost_cents": b.cost_cents, "created_at": b.created_at.isoformat()}
        for b in batches
    ]}


@router.get("/api/batches/{batch_id}/roi")
async def batch_roi(batch_id: str, session: AsyncSession = Depends(get_session)):
    """Full ROI breakdown for a single lead batch."""
    from domain.batch_roi import compute_batch_roi
    repo = BatchRepo(session)
    batch = await repo.get(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="batch not found")
    stats = await repo.roi_stats(batch_id)
    roi = compute_batch_roi(
        batch_id=batch_id,
        vendor_name=batch.vendor_name,
        source_tag=batch.source_tag,
        cost_cents=batch.cost_cents,
        **stats,
    )
    return {**roi.__dict__, "verdict": roi.verdict()}


@router.get("/api/lead-sources/roi")
async def lead_source_roi(session: AsyncSession = Depends(get_session)):
    """Cross-batch ROI ranked by AP return — use this to decide where to spend tomorrow."""
    repo = BatchRepo(session)
    rows = await repo.source_roi_summary(pinned_tenant_id())
    for r in rows:
        c = r["total_cost_cents"]
        r["verdict"] = (
            "scale_immediately" if r["roi_multiplier"] >= 10 else
            "strong"      if r["roi_multiplier"] >= 5  else
            "profitable"  if r["roi_multiplier"] >= 3  else
            "marginal"    if r["roi_multiplier"] >= 1  else
            "kill_vendor"
        )
        r["cost_per_lead_dollars"] = round(c / r["lead_count"] / 100, 2) if r["lead_count"] else 0
    return {"sources": rows}


# ── Real-time quoting ─────────────────────────────────────────────────────────

@router.post("/api/quote")
async def get_quote(request: Request, session: AsyncSession = Depends(get_session)):
    """
    Real-time carrier quoting. Hits rate tables (FE) or CMS API (Medicare) with formula fallback.
    Used directly by the AI agent's get_quote tool.

    When the payload carries the prospect's stated need (coverage_purpose /
    burial_or_cremation / desired_coverage_amount / monthly_budget), the
    response also carries three deterministic options (C, B, A) sized by the
    quote engine — the model never picks a face amount or premium itself.
    """
    from domain.quoting import (
        CmsMedicareAdapter,
        FormulaAdapter,
        QuoteManager,
        QuoteRequest,
        RateTableAdapter,
        build_quote_options,
    )

    body: dict = await request.json()
    settings = get_settings()

    if settings.prospect_quotes_require_verified:
        return await _verified_quote(body, session, settings)

    face_value_dollars = body.get("face_value", 10_000)
    condition_codes: set[str] = set(body.get("condition_codes", []))
    tobacco_raw = body.get("tobacco", False)
    tobacco = tobacco_raw if isinstance(tobacco_raw, bool) else str(tobacco_raw).lower() in ("true", "yes", "1", "current")

    req = QuoteRequest(
        lead_id=body.get("lead_id", "api-test"),
        product_line=body.get("product_line", "final_expense"),
        age=body.get("age"),
        state=body.get("state"),
        zip_code=body.get("zip_code"),
        county=body.get("county"),
        face_value=int(face_value_dollars * 100),
        tobacco=tobacco,
        gender=body.get("gender"),
        condition_codes=condition_codes,
        rate_tier=body.get("rate_tier"),
    )

    cms_target = [c.strip() for c in settings.cms_target_carriers.split(",") if c.strip()]
    manager = QuoteManager(
        adapters=[
            CmsMedicareAdapter(
                ma_dataset_id=settings.cms_ma_dataset_id,
                ms_dataset_id=settings.cms_ms_dataset_id or None,
                target_carriers=cms_target,
                timeout=settings.quote_timeout_seconds,
            ),
            RateTableAdapter(session=session),
            FormulaAdapter(),
        ],
        timeout=settings.quote_timeout_seconds,
    )

    response = await manager.get_quotes(req)
    best = response.best

    def _source_label(src: str) -> str:
        return "estimate" if src == "formula" else src

    def _option(q) -> dict:
        # q.labels() carries quote_type / rate_source / carrier_rate / bindable
        # so a formula premium can never leave core looking like a carrier rate.
        return {
            "carrier":         q.carrier,
            "monthly_premium": q.monthly_premium,
            "rate_tier":       q.rate_tier,
            "plan_name":       q.plan_name,
            "source":          _source_label(q.source),
            **q.labels(),
        }

    # Three-option (C/B/A) generation for life products. The engine — never the
    # model — sizes the faces from the stated need and budget, quotes each face
    # through the same adapter stack, and orders them for presentation.
    def _amount(key: str) -> int | None:
        v = body.get(key)
        try:
            n = int(float(v))
            return n if n > 0 else None
        except (ValueError, TypeError):
            return None

    tiered_options: list[dict] = []
    if req.product_line in ("final_expense", "life"):
        try:
            opts = await build_quote_options(
                manager,
                req,
                coverage_purpose=body.get("coverage_purpose"),
                burial_or_cremation=body.get("burial_or_cremation"),
                desired_amount=_amount("desired_coverage_amount"),
                monthly_budget=_amount("monthly_budget"),
                min_face=settings.quote_min_face_dollars,
                max_face=settings.quote_max_face_dollars,
            )
            tiered_options = [o.to_payload() for o in opts]
            for o in tiered_options:
                o["source"] = "estimate" if o.get("quote_type") != "carrier" else o.get("rate_source")
        except Exception:
            logger.exception("three-option quote build failed for lead %s", req.lead_id)
            tiered_options = []

    return {
        "quote_source": _source_label(response.source),
        "fetched_at": response.fetched_at,
        **response.labels(),
        "best": _option(best) if best else None,
        "all_options": [_option(q) for q in response.quotes],
        # Presentation order C, B, A. Empty when the engine could not size or
        # quote the tiers — the caller must fall back honestly, never invent.
        "options": tiered_options,
        "errors": response.errors,
    }


async def _verified_quote(body: dict, session: AsyncSession, settings) -> dict:
    """Quotes a prospect may hear: exact premiums from VERIFIED carrier rate
    sheets only, each option stored (quote_id / quote_ref) before it is
    returned. No verified rate → no number, with the reasons — the caller must
    offer the licensed agent instead of estimating."""
    import uuid as _uuid
    from datetime import datetime as _dt, timezone as _tz
    from db.models import Lead as _Lead
    from domain.quoting.prospect import quote_for_prospect

    product = body.get("product_line", "final_expense")
    tob_raw = body.get("tobacco")
    tobacco = None if tob_raw is None else (tob_raw if isinstance(tob_raw, bool)
                                            else str(tob_raw).lower() in ("true", "yes", "1", "current"))
    sex = (str(body.get("gender") or body.get("sex") or "").strip().upper()[:1] or None)

    def _int(key):
        try:
            n = int(float(body.get(key)))
            return n if n > 0 else None
        except (TypeError, ValueError):
            return None

    base = {"quote_policy": "verified_rate_sheets_only", "fetched_at": _dt.now(_tz.utc).isoformat(),
            "quote_type": "estimated", "rate_source": "none", "carrier_rate": False, "bindable": False,
            "best": None, "all_options": [], "options": [], "errors": []}
    try:
        lead_uuid = _uuid.UUID(str(body.get("lead_id")))
        lead = await session.get(_Lead, lead_uuid)
    except (ValueError, TypeError):
        lead = None
    if lead is None or lead.tenant_id != pinned_tenant_id():
        return {**base, "unavailable_reasons": ["a quote is stored against a lead; lead_id is unknown"]}
    if product not in ("final_expense", "life"):
        return {**base, "unavailable_reasons": [f"no verified rate source for {product}"]}

    result = await quote_for_prospect(
        session, tenant_id=lead.tenant_id, lead_id=lead.id, product_line=product,
        state=body.get("state") or lead.state, age=body.get("age") or lead.age,
        sex=sex if sex in ("M", "F") else (lead.sex if lead.sex in ("M", "F") else None),
        tobacco=tobacco if tobacco is not None else lead.tobacco, tier=body.get("rate_tier"),
        desired_face=_int("desired_coverage_amount"), purpose=body.get("burial_or_cremation") or body.get("coverage_purpose"),
        monthly_budget=_int("monthly_budget"),
    )
    await session.commit()
    if not result.ok:
        return {**base, "unavailable_reasons": result.unavailable_reasons, "missing": result.missing}

    def _payload(o) -> dict:
        source = "rate_sheet_test" if o.is_test else "rate_sheet"
        return {"label": o.label, "face_value": o.face, "monthly_premium": float(o.monthly_premium),
                "carrier": o.carrier, "plan_name": o.plan_name, "rate_tier": o.tier,
                "within_budget": o.within_budget, "quote_id": o.quote_id, "quote_ref": o.quote_ref,
                "quote_type": "test" if o.is_test else "carrier", "rate_source": source,
                "carrier_rate": not o.is_test, "bindable": False, "preliminary": True, "source": source}

    options = [_payload(o) for o in result.options]
    best = options[min(1, len(options) - 1)]
    return {**base, "quote_type": best["quote_type"], "rate_source": best["rate_source"],
            "carrier_rate": best["carrier_rate"], "quote_source": best["rate_source"],
            "best": best, "all_options": options, "options": options,
            "unavailable_reasons": result.unavailable_reasons}


# ── Appointment booking ───────────────────────────────────────────────────────

@router.get("/api/scheduling/availability")
async def scheduling_availability(timezone: str | None = None, limit: int = 3,
                                  session: AsyncSession = Depends(get_session)):
    """
    Real upcoming appointment times from the configured scheduling provider
    (Calendly). The voice agent calls this BEFORE offering any time to a
    prospect — offered slots are actual availability, never invented.

    Fail-soft: when Calendly is disabled or unreachable the response says so and
    returns no times; the agent falls back to collecting a preference honestly.
    """
    settings = get_settings()
    provider = _make_scheduling_provider(settings)
    if provider is None:
        calendar = _local_calendar(settings)
        if calendar is not None:
            # The agent's own part-time calendar: real open slots, overbooking-proof.
            times = await calendar.available_times(session, pinned_tenant_id(), limit=max(limit, 1))
            return {"calendly_enabled": False, "calendar": "local", "requires_email": False,
                    "times": times, "timezone": settings.calendly_default_timezone}
        return {"calendly_enabled": False, "times": [], "timezone": settings.calendly_default_timezone}

    from domain.scheduling import CalendlyUnavailableError

    tz = timezone or settings.calendly_default_timezone
    try:
        times = await provider.available_times(timezone_name=tz)
    except CalendlyUnavailableError as exc:
        logger.warning("calendly availability unavailable: %s", exc)
        return {"calendly_enabled": True, "times": [], "timezone": tz, "error": "unavailable"}

    return {"calendly_enabled": True, "times": times[: max(limit, 1)], "timezone": tz}


@router.post("/api/appointments", status_code=201)
async def create_appointment(request: Request, session: AsyncSession = Depends(get_session)):
    """
    Create an appointment from the AI agent's booking request.

    When Calendly is enabled and the agent sends a selected slot (start_time +
    prospect email), the slot is booked through the Calendly Scheduling API and
    the identifiers are persisted. A Calendly failure still persists the local
    appointment request and profile — with calendly_status="failed" — so Ryan
    gets the requested time, but no confirmed-appointment alert is sent and the
    prospect is never told the calendar booking succeeded.

    The appointment brief SMS sends only after the local write commits AND
    (when Calendly was requested) Calendly confirmed the booking. Exactly once.
    """
    import uuid as _uuid
    from datetime import datetime, timedelta, timezone
    from db.models import Appointment as _ApptModel, BookingSlot as _SlotModel, HandoffProfile as _ProfileModel

    body: dict = await request.json()
    settings = get_settings()

    lead_id_str = body.get("lead_id")
    session_id = body.get("session_id", "")
    preferred_date = body.get("preferred_date")
    preferred_time = body.get("preferred_time", "10:00")
    start_time_iso = body.get("start_time")  # ISO slot chosen from availability
    prospect_email = body.get("email")
    tz_name = body.get("timezone") or settings.calendly_default_timezone
    notes = body.get("notes")
    prospect_profile = body.get("prospect_profile")

    if not lead_id_str or not (preferred_date or start_time_iso):
        raise HTTPException(status_code=422, detail="lead_id and a date/time are required")

    # ── Calendly booking happens BEFORE the local write, so what we persist is
    # what actually happened on the calendar.
    provider = _make_scheduling_provider(settings)
    calendly_requested = provider is not None and bool(start_time_iso)
    calendly_booking = None
    calendly_status = "not_requested"
    if calendly_requested:
        if not prospect_email:
            raise HTTPException(status_code=422, detail="email_required")
        from domain.scheduling import CalendlyUnavailableError

        prospect_name = ""
        if prospect_profile:
            prospect_name = str(prospect_profile.get("full_name") or "").replace("unknown", "").strip()
        try:
            calendly_booking = await provider.create_booking(
                start_time=start_time_iso,
                name=prospect_name or "Eastwood prospect",
                email=prospect_email,
                timezone_name=tz_name,
                phone=(prospect_profile or {}).get("phone_e164"),
            )
            calendly_status = "confirmed"
        except CalendlyUnavailableError as exc:
            logger.warning("calendly booking failed for lead %s: %s", lead_id_str, exc)
            calendly_status = "failed"

    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        booking_tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        booking_tz = ZoneInfo(settings.calendly_default_timezone)
        tz_name = settings.calendly_default_timezone

    if start_time_iso:
        try:
            starts_at = datetime.fromisoformat(start_time_iso.replace("Z", "+00:00"))
            if starts_at.tzinfo is None:
                starts_at = starts_at.replace(tzinfo=booking_tz)
        except ValueError:
            raise HTTPException(status_code=422, detail="start_time must be ISO 8601") from None
    else:
        # A spoken preference is in the prospect's/agent's local time — never UTC.
        try:
            starts_at = datetime.fromisoformat(f"{preferred_date}T{preferred_time}").replace(tzinfo=booking_tz)
        except ValueError:
            raise HTTPException(status_code=422, detail="preferred_date/preferred_time not understood") from None

    calendar = _local_calendar(settings) if provider is None else None
    if calendar is not None and start_time_iso:
        # Reserve on the agent's own calendar (advisory lock + unique index).
        from domain.scheduling.local import SlotUnavailableError
        from sqlalchemy.exc import IntegrityError

        try:
            slot = await calendar.reserve(session, settings.default_tenant_id, starts_at)
        except SlotUnavailableError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail={"error": "slot_unavailable", "reason": exc.reason}) from None
        except IntegrityError:
            await session.rollback()
            raise HTTPException(status_code=409, detail={"error": "slot_unavailable", "reason": "slot_taken"}) from None
        starts_at = slot.starts_at
    else:
        # Calendly-confirmed bookings hold the time; a free-form request (no
        # calendar, or a preference outside offered slots) does not block it.
        holds_time = calendly_status == "confirmed"
        slot = _SlotModel(
            tenant_id=settings.default_tenant_id,
            starts_at=starts_at.astimezone(timezone.utc),
            ends_at=starts_at.astimezone(timezone.utc) + timedelta(minutes=settings.appointment_slot_minutes),
            available=not holds_time,
        )
        session.add(slot)
        await session.flush()

    appointment = _ApptModel(
        lead_id=_uuid.UUID(lead_id_str),
        slot_id=slot.id,
        tenant_id=settings.default_tenant_id,
        status=(
            "scheduled" if (calendar is not None and start_time_iso) or calendly_status == "confirmed"
            else "requested"
        ),
        notes=notes,
        prospect_email=prospect_email,
        timezone=tz_name,
        calendly_status=calendly_status,
        calendly_event_uri=calendly_booking.event_uri if calendly_booking else None,
        calendly_invitee_uri=calendly_booking.invitee_uri if calendly_booking else None,
        calendly_cancel_url=calendly_booking.cancel_url if calendly_booking else None,
        calendly_reschedule_url=calendly_booking.reschedule_url if calendly_booking else None,
    )
    session.add(appointment)
    await session.flush()

    # Persist the full profile as the canonical handoff record, in the same
    # transaction as the appointment — the SMS is just the alert.
    if prospect_profile:
        prospect_profile = {
            **prospect_profile,
            "calendly_status": calendly_status,
            "appointment_time": (
                f"{starts_at.isoformat()} {tz_name}" if start_time_iso
                else prospect_profile.get("appointment_time") or f"{preferred_date} {preferred_time} {tz_name}"
            ),
        }
        session.add(_ProfileModel(
            lead_id=_uuid.UUID(lead_id_str),
            appointment_id=appointment.id,
            tenant_id=settings.default_tenant_id,
            session_id=session_id,
            profile=prospect_profile,
        ))
        await session.flush()

    # If this raises, we never reach the notify step below — no success alert
    # is sent for a booking that did not persist.
    await session.commit()

    await publish(
        settings.topic_agent_events,
        {
            "eventType": "appointment_booked",
            "leadId": lead_id_str,
            "sessionId": session_id,
            "slotId": str(slot.id),
        },
        key=lead_id_str,
    )

    # Only after the write succeeds — and never for a failed Calendly booking:
    # a "NEW EASTWOOD APPOINTMENT" alert for a slot that is not on the calendar
    # would be a false confirmation. Ryan still learns about the requested time
    # from the call-outcome SMS and the persisted profile. Fail-soft: a missing
    # destination or SMS error never breaks the prospect-facing booking.
    if prospect_profile and calendly_status != "failed":
        try:
            _make_appointment_notifier(settings).send_agent_brief(
                prospect_profile, appointment_id=str(appointment.id)
            )
        except Exception:
            logger.exception("agent brief send failed for appointment %s", appointment.id)

    return {
        "appointment_id": str(appointment.id),
        "booked_at": starts_at.isoformat(),
        "date": starts_at.astimezone(booking_tz).date().isoformat(),
        "time": starts_at.astimezone(booking_tz).strftime("%H:%M"),
        "timezone": tz_name,
        "status": appointment.status,
        "calendly_status": calendly_status,
        "calendly_event_uri": calendly_booking.event_uri if calendly_booking else None,
        "calendly_invitee_uri": calendly_booking.invitee_uri if calendly_booking else None,
        "calendly_cancel_url": calendly_booking.cancel_url if calendly_booking else None,
        "calendly_reschedule_url": calendly_booking.reschedule_url if calendly_booking else None,
    }


def _make_appointment_notifier(settings):
    """Build the handoff notifier from settings. Factored out so it is injectable."""
    from clients.twilio_sms import SmsClient
    from domain.notifications import AppointmentNotificationService

    sms = SmsClient() if settings.twilio_account_sid else None
    return AppointmentNotificationService(sms, settings.agent_notification_phone_e164)


def _local_calendar(settings):
    """The agent's own availability calendar, or None when AGENT_AVAILABILITY is unset."""
    from domain.scheduling.local import LocalCalendar

    return LocalCalendar.from_settings(settings)


def _make_scheduling_provider(settings):
    """Scheduling provider from settings. Factored out so tests can inject one."""
    from domain.scheduling import make_scheduling_provider

    return make_scheduling_provider(settings)


# ── Acquisition agenda ────────────────────────────────────────────────────────

@router.post("/api/agenda/followups")
async def agenda_followups(request: Request, session: AsyncSession = Depends(get_session)):
    """
    Return Tier 2 follow-up questions for a condition the prospect just confirmed.
    Skips any questions already present in the lead's health_data.
    """
    import uuid as _uuid
    from domain.acquisition import get_followup_tasks, plan_to_context
    from db.models import Lead as _Lead

    body: dict = await request.json()
    condition = body.get("condition", "").lower().strip()
    lead_id_str = body.get("lead_id", "")

    if not condition:
        raise HTTPException(status_code=422, detail="condition is required")

    health_data: dict = {}
    if lead_id_str:
        try:
            from sqlalchemy import select as _sel
            row = await session.execute(_sel(_Lead).where(_Lead.id == _uuid.UUID(lead_id_str)))
            lead_row = row.scalar_one_or_none()
            if lead_row:
                health_data = lead_row.health_data or {}
        except Exception:
            pass

    tasks = get_followup_tasks(condition, health_data)
    return {"condition": condition, "questions": plan_to_context(tasks)}


# ── Carrier placement ─────────────────────────────────────────────────────────

@router.post("/api/triage/place")
async def placement_endpoint(request: Request):
    """
    Field underwriting assistant — returns ranked carrier options + missing questions.
    Feed this into the acquisition plan before the AI agent calls.
    """
    from domain.placement import place
    body: dict = await request.json()
    hd = body.get("health_data", {})
    result = place(
        lead_id=body.get("lead_id", ""),
        age=body.get("age"),
        state=body.get("state"),
        product_line=body.get("product_line"),
        condition_codes=set(hd.get("condition_codes", [])),
        medication_codes=set(hd.get("medication_codes", [])),
        tobacco=hd.get("tobacco", "unknown"),
        face_value=body.get("face_value", 10_000),
        already_captured=set(body.get("already_captured", [])),
        triage_bucket=body.get("triage_bucket"),
    )
    return {
        "top_carrier":           result.top_carrier,
        "top_tier":              result.top_tier,
        "gi_available":          result.gi_available,
        "placement_ready":       result.placement_ready,
        "missing_questions":     result.missing_questions,
        "all_required_questions": result.all_required_questions,
        "options": [
            {
                "carrier":              o.carrier,
                "rate_tier":            o.rate_tier,
                "confidence":           o.confidence,
                "monthly_premium_est":  o.monthly_premium_est,
                "missing_questions":    o.missing_questions,
                "notes":                o.placement_notes,
            }
            for o in result.options[:5]
        ],
    }


# ── Call outcome tagging ──────────────────────────────────────────────────────

@router.post("/api/calls/{call_id}/outcome")
async def tag_call_outcome(call_id: str, request: Request, session: AsyncSession = Depends(get_session)):
    """
    Tag a call outcome and trigger the appropriate follow-up sequence.
    outcome values: no_answer | voicemail | interested_busy | quoted_not_closed |
                    price_objection | spouse_objection | application_not_completed |
                    payment_failed | sold | dnc | not_interested | callback
    """
    import uuid as _uuid
    from repos.repos import FollowupRepo
    from domain.followup import schedule_for_outcome

    body: dict = await request.json()
    outcome = body.get("outcome", "")

    call_repo = CallRepo(session)
    await call_repo.update_outcome(
        _uuid.UUID(call_id),
        status="tagged",
        outcome=outcome,
        duration=body.get("duration_seconds"),
    )

    # Tag AP on the lead when sold
    if outcome == "sold" and (ap := body.get("ap_cents")) and body.get("lead_id"):
        from sqlalchemy import update as _update
        from db.models import Lead as _Lead
        await session.execute(
            _update(_Lead).where(_Lead.id == _uuid.UUID(body["lead_id"])).values(ap_cents=ap)
        )

    followup_repo = FollowupRepo(session)
    n = await schedule_for_outcome(
        lead_id=_uuid.UUID(body["lead_id"]),
        tenant_id=pinned_tenant_id(),
        outcome=outcome,
        context_data=body.get("context", {}),
        followup_repo=followup_repo,
    )
    return {"tagged": True, "outcome": outcome, "followup_tasks_scheduled": n}


# ── Human closer cockpit ──────────────────────────────────────────────────────

@router.get("/api/cockpit/queue")
async def cockpit_queue(
    limit: int = 25,
    session: AsyncSession = Depends(get_session),
):
    """
    The dialer queue — every lead worth calling right now, sorted best-first.
    Includes placement suggestion, health flags, and a suggested opener.
    Ryan should never have to hunt for context.
    """
    from sqlalchemy import select as _select
    from db.models import Lead as _Lead
    from domain.placement import place

    t_id = pinned_tenant_id()

    result = await session.execute(
        _select(_Lead)
        .where(
            _Lead.tenant_id == t_id,
            _Lead.triage_bucket.in_(["CALL_NOW", "WORKABLE"]),
            _Lead.status == "queued",
        )
        .order_by(_Lead.triage_priority.desc().nullslast())
        .limit(limit)
    )
    leads = list(result.scalars())

    queue = []
    for lead in leads:
        hd = lead.health_data or {}
        conditions = [c["code"] for c in hd.get("conditions", []) if "code" in c]
        meds = [m["code"] for m in hd.get("medications", []) if "code" in m]

        placement = place(
            lead_id=str(lead.id),
            age=lead.age,
            state=lead.state,
            product_line=lead.product_line,
            condition_codes=set(conditions),
            medication_codes=set(meds),
            tobacco=hd.get("tobacco", "unknown"),
            already_captured=set(hd.keys()) | ({"age"} if lead.age else set()),
        )

        opener = _build_opener(lead, placement, hd)

        queue.append({
            "lead_id":       str(lead.id),
            "name":          f"{lead.first_name or ''} {lead.last_name or ''}".strip() or "Unknown",
            "age":           lead.age,
            "state":         lead.state,
            "phone":         lead.phone_e164,
            "product":       lead.product_line,
            "triage_bucket": lead.triage_bucket,
            "priority":      lead.triage_priority,
            "health_flags":  conditions[:4],
            "tobacco":       hd.get("tobacco", "unknown"),
            "placement": {
                "top_carrier":  placement.top_carrier,
                "top_tier":     placement.top_tier,
                "gi_available": placement.gi_available,
                "est_premium":  placement.options[0].monthly_premium_est if placement.options else None,
            },
            "missing_questions": placement.missing_questions[:3],
            "suggested_opener":  opener,
            "source_batch_id":   lead.source_batch_id,
        })

    return {"count": len(queue), "queue": queue}


@router.get("/api/cockpit/lead/{lead_id}")
async def cockpit_lead(lead_id: str, session: AsyncSession = Depends(get_session)):
    """Full prep sheet for a single lead — everything Ryan needs before saying hello."""
    import uuid as _uuid
    from sqlalchemy import select as _select
    from db.models import Lead as _Lead, CallRecord as _CallRecord, QuoteRecord as _QuoteRecord
    from domain.placement import place

    result = await session.execute(_select(_Lead).where(_Lead.id == _uuid.UUID(lead_id)))
    lead = result.scalar_one_or_none()
    if not lead:
        raise HTTPException(status_code=404, detail="lead not found")

    hd = lead.health_data or {}
    conditions = [c["code"] for c in hd.get("conditions", []) if "code" in c]
    meds = [m["code"] for m in hd.get("medications", []) if "code" in m]

    placement = place(
        lead_id=lead_id,
        age=lead.age,
        state=lead.state,
        product_line=lead.product_line,
        condition_codes=set(conditions),
        medication_codes=set(meds),
        tobacco=hd.get("tobacco", "unknown"),
        already_captured=set(hd.keys()) | ({"age"} if lead.age else set()),
    )

    # Last 3 call records
    calls_result = await session.execute(
        _select(_CallRecord)
        .where(_CallRecord.lead_id == lead.id)
        .order_by(_CallRecord.created_at.desc())
        .limit(3)
    )
    recent_calls = [
        {"outcome": c.outcome, "duration": c.duration_seconds,
         "summary": c.ai_summary, "started_at": c.started_at.isoformat() if c.started_at else None}
        for c in calls_result.scalars()
    ]

    # Last quote
    quote_result = await session.execute(
        _select(_QuoteRecord)
        .where(_QuoteRecord.lead_id == lead.id)
        .order_by(_QuoteRecord.presented_at.desc())
        .limit(1)
    )
    last_quote = quote_result.scalar_one_or_none()

    return {
        "lead_id":   lead_id,
        "name":      f"{lead.first_name or ''} {lead.last_name or ''}".strip(),
        "age":       lead.age,
        "state":     lead.state,
        "phone":     lead.phone_e164,
        "product":   lead.product_line,
        "triage": {
            "bucket":   lead.triage_bucket,
            "priority": lead.triage_priority,
        },
        "health": {
            "conditions": conditions,
            "medications": meds,
            "tobacco":     hd.get("tobacco", "unknown"),
            "budget":      hd.get("monthly_budget"),
            "beneficiary": hd.get("beneficiary_name"),
        },
        "placement": {
            "top_carrier":        placement.top_carrier,
            "top_tier":           placement.top_tier,
            "gi_available":       placement.gi_available,
            "placement_ready":    placement.placement_ready,
            "missing_questions":  placement.missing_questions,
            "options": [
                {"carrier": o.carrier, "tier": o.rate_tier,
                 "confidence": o.confidence, "est_premium": o.monthly_premium_est,
                 "notes": o.placement_notes}
                for o in placement.options[:3]
            ],
        },
        "last_quote": {
            "carrier":        last_quote.carrier,
            "product":        last_quote.product_line,
            "monthly_premium": float(last_quote.monthly_premium or 0),
            "presented_at":   last_quote.presented_at.isoformat(),
        } if last_quote else None,
        "recent_calls":      recent_calls,
        "suggested_opener":  _build_opener(lead, placement, hd),
        "source_batch_id":   lead.source_batch_id,
    }


def _build_opener(lead, placement, hd: dict) -> str:
    """One-sentence suggested opener personalized to what we know."""
    name = lead.first_name or "there"
    product = (lead.product_line or "final expense").replace("_", " ")

    if placement.top_tier == "level" and not placement.missing_questions:
        carrier = placement.top_carrier or "our top carrier"
        est = placement.options[0].monthly_premium_est if placement.options else None
        premium_hint = f" around ${est:.0f}/month" if est else ""
        return (
            f"Hi {name}, based on what I can see I think I have a level-benefit plan through "
            f"{carrier}{premium_hint} — let me confirm a couple quick things and I can get you a firm number."
        )
    if placement.top_tier in ("graded", "modified"):
        return (
            f"Hi {name}, I've got some good options for your situation on {product} — "
            f"let me ask a couple of quick health questions so I can find the best fit."
        )
    if placement.gi_available:
        return (
            f"Hi {name}, I have a guaranteed-issue plan that doesn't require any health questions — "
            f"let me walk you through what that looks like."
        )
    return (
        f"Hi {name}, I'm following up on your {product} inquiry — "
        f"do you have a few minutes to go over your options?"
    )


@router.post("/twilio/status/{session_id}")
async def twilio_status_callback(
    session_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Twilio fires this when a call reaches a terminal status.

    Frees the concurrency slot, schedules a retry for an unanswered call when the
    lead is still eligible, and closes the ai-agent session immediately."""
    from domain.dialing import handle_terminal_status

    form = await request.form()
    call_status = str(form.get("CallStatus", ""))

    result = await handle_terminal_status(
        session, session_id, call_status, ai_agent=_get_ai_agent_client()
    )

    # Close the attempt and book its spend (idempotent; separate transaction).
    from domain.attempts import close_attempt
    from domain.dialing import TWILIO_STATUS_MAP

    normalized = TWILIO_STATUS_MAP.get(call_status)
    if normalized:
        from db.session import session_scope

        try:
            async with session_scope() as spend_session:
                closed = await close_attempt(
                    spend_session, session_id=session_id, status=normalized,
                    duration_seconds=form.get("CallDuration"), answered_by=form.get("AnsweredBy"),
                )
            if closed:
                result["cost_usd"] = closed["cost_usd"]
        except Exception:
            logger.exception("could not close dial attempt for session %s", session_id)
    logger.info("twilio status callback: session=%s status=%s result=%s", session_id, call_status, result)
    return result


@router.post("/twilio/sms")
async def twilio_inbound_sms(request: Request, session: AsyncSession = Depends(get_session)):
    """Inbound SMS webhook (configure as the Twilio number's "A message comes in"
    URL: {PUBLIC_BASE_URL}/twilio/sms). Signature-validated like every /twilio
    route. A texted opt-out suppresses the number for calls and texts and
    cancels everything pending for it. Returns empty TwiML — Twilio's own
    opt-out handling sends the carrier-required confirmation."""
    from starlette.responses import Response as StarletteResponse

    from domain.compliance.sms_opt_out import apply_opt_out, is_opt_out
    from domain.normalization import normalize_phone

    form = await request.form()
    phone = normalize_phone(str(form.get("From", "")))
    body = str(form.get("Body", ""))
    if phone and not is_opt_out(body):
        engine = getattr(request.app.state, "sms_engine", None)
        if engine is not None:
            try:
                segs = int(str(form.get("NumSegments") or 1))
            except ValueError:
                segs = 1
            result = await engine.handle_inbound(phone=phone, body=body, num_segments=segs,
                                                 provider_sid=str(form.get("MessageSid") or ""))
            logger.info("inbound SMS handled: %s", {k: v for k, v in result.items() if k != "phone"})
    if phone and is_opt_out(body):
        try:
            segs = int(str(form.get("NumSegments") or 1))
        except ValueError:
            segs = 1
        result = await apply_opt_out(
            session, pinned_tenant_id(), phone,
            note=f"SMS opt-out, MessageSid={str(form.get('MessageSid', ''))[:64]}",
            body=body, provider_sid=str(form.get("MessageSid") or "")[:64] or None, segments=segs,
        )
        await session.commit()
        logger.warning(
            "inbound SMS opt-out processed: leads=%d cancelled_dials=%d cancelled_followups=%d new=%s",
            result["leads"], result["cancelled_dials"], result["cancelled_followups"],
            result["newly_suppressed"],
        )
    return StarletteResponse(
        content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
        media_type="application/xml",
    )


@router.post("/twilio/sms-status/{message_id}")
async def twilio_sms_status(message_id: str, request: Request, session: AsyncSession = Depends(get_session)):
    """Delivery receipts for outbound texts (MessageStatus: sent/delivered/undelivered/failed)."""
    import uuid as _uuid
    from db.models import SmsMessage as _Msg

    form = await request.form()
    status_value = str(form.get("MessageStatus", ""))[:16]
    try:
        msg = await session.get(_Msg, _uuid.UUID(message_id))
    except ValueError:
        msg = None
    # Receipts can arrive out of order ("delivered" before "sent"): never step back.
    rank = {"sending": 0, "queued": 0, "accepted": 0, "sent": 1, "undelivered": 2, "failed": 2, "delivered": 3}
    if msg is not None and status_value in rank and rank[status_value] >= rank.get(msg.status, -1):
        msg.status = status_value
        await session.commit()
    return {"received": True}


_ai_agent_client = None


def _get_ai_agent_client():
    """Module-level AiAgentClient, reused across callbacks (holds one httpx client)."""
    global _ai_agent_client
    if _ai_agent_client is None:
        from clients.ai_agent import AiAgentClient
        _ai_agent_client = AiAgentClient()
    return _ai_agent_client


@router.get("/api/dashboard/summary")
async def dashboard_summary(
    session: AsyncSession = Depends(get_session),
):
    from sqlalchemy import func, select
    from db.models import Lead

    t_id = pinned_tenant_id()

    result = await session.execute(
        select(Lead.score_band, func.count(Lead.id))
        .where(Lead.tenant_id == t_id)
        .group_by(Lead.score_band)
    )
    band_counts = {row[0]: row[1] for row in result}

    status_result = await session.execute(
        select(Lead.status, func.count(Lead.id))
        .where(Lead.tenant_id == t_id)
        .group_by(Lead.status)
    )
    status_counts = {row[0]: row[1] for row in status_result}

    return {
        "tenant_id": t_id,
        "by_band": band_counts,
        "by_status": status_counts,
    }


# ── Outbound campaign control (arm / authorize / emergency halt) ──────────────

def _control_body_fields(body: Any) -> tuple[str, str]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="JSON object body required")
    operator = str(body.get("operator") or "").strip()
    reason = str(body.get("reason") or "").strip()
    if not operator or not reason:
        raise HTTPException(status_code=422, detail="operator and reason are required")
    return operator[:128], reason[:2000]


@router.get("/api/outbound/status")
async def outbound_status(session: AsyncSession = Depends(get_session)):
    """Whether prospect-facing outbound is permitted, and who last changed it."""
    from domain.outbound import get_outbound_state

    state = await get_outbound_state(session, pinned_tenant_id())
    return state.as_dict()


@router.post("/api/outbound/halt")
async def outbound_halt(request: Request, session: AsyncSession = Depends(get_session)):
    """Emergency stop. Always accepted. The dialer stops on its next pass (≤3s)
    and the follow-up worker on its next pass (≤30s); calls already connected
    finish normally."""
    from domain.outbound import halt_outbound

    try:
        body = await request.json()
    except Exception:
        body = None
    operator, reason = _control_body_fields(body)
    state = await halt_outbound(session, operator=operator, reason=reason, tenant_id=pinned_tenant_id())
    await session.commit()
    return state.as_dict()


@router.post("/api/outbound/resume")
async def outbound_resume(request: Request, session: AsyncSession = Depends(get_session)):
    """Authorize the campaign (the runtime half of the two-key launch).
    409 when the deployment is not armed (OUTBOUND_ENABLED=false)."""
    from domain.outbound import OutboundNotArmedError, resume_outbound

    try:
        body = await request.json()
    except Exception:
        body = None
    operator, reason = _control_body_fields(body)
    try:
        state = await resume_outbound(session, operator=operator, reason=reason, tenant_id=pinned_tenant_id())
    except OutboundNotArmedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    return state.as_dict()


# ── Imports, inventory, campaigns, reporting ──────────────────────────────────

@router.get("/api/imports")
async def list_imports(session: AsyncSession = Depends(get_session)):
    from sqlalchemy import select as _select
    from db.models import ImportBatch

    rows = (await session.execute(
        _select(ImportBatch).where(ImportBatch.tenant_id == pinned_tenant_id()).order_by(ImportBatch.started_at.desc())
    )).scalars()
    return {"imports": [{
        "batch_id": str(b.id), "file_name": b.file_name, "status": b.status, "source_name": b.source_name,
        "total_rows": b.total_rows, "inserted": b.inserted, "quarantined": b.quarantined,
        "authorized_ai_voice": b.authorized_ai_voice, "started_at": b.started_at.isoformat() if b.started_at else None,
    } for b in rows]}


@router.get("/api/imports/{batch_id}")
async def import_report(batch_id: str):
    from domain.imports import batch_report

    try:
        report = await batch_report(batch_id)
    except ValueError:
        report = None
    if report is None:
        raise HTTPException(status_code=404, detail="import batch not found")
    return report


@router.get("/api/imports/{batch_id}/errors.csv")
async def import_errors_csv(batch_id: str):
    from starlette.responses import Response as StarletteResponse
    from domain.imports import errors_csv

    try:
        body = await errors_csv(batch_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="import batch not found") from None
    return StarletteResponse(content=body, media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="import-{batch_id}-errors.csv"'})


@router.get("/api/inventory")
async def inventory(import_batch_id: str | None = None, format: str = "json",
                    session: AsyncSession = Depends(get_session)):
    """Inventory by state, area code, product, lead age, consent and eligibility,
    with FTC registry cost computed from the actual area codes."""
    from domain.inventory import build_inventory, inventory_markdown

    inv = await build_inventory(session, pinned_tenant_id(), import_batch_id=import_batch_id)
    if format == "md":
        from starlette.responses import Response as StarletteResponse

        return StarletteResponse(content=inventory_markdown(inv), media_type="text/markdown")
    return inv


async def _campaign_or_404(session, campaign_id: str):
    from domain.campaigns import get_campaign

    c = await get_campaign(session, campaign_id, pinned_tenant_id())
    if c is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    return c


@router.post("/api/campaigns", status_code=201)
async def create_campaign_route(request: Request, session: AsyncSession = Depends(get_session)):
    from domain.campaigns import CampaignError, campaign_stats, create_campaign

    try:
        c = await create_campaign(session, await request.json(), pinned_tenant_id())
    except CampaignError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await session.commit()
    return await campaign_stats(session, c)


@router.get("/api/campaigns")
async def list_campaigns(session: AsyncSession = Depends(get_session)):
    from sqlalchemy import select as _select
    from db.models import Campaign
    from domain.campaigns import campaign_stats

    rows = list((await session.execute(
        _select(Campaign).where(Campaign.tenant_id == pinned_tenant_id()).order_by(Campaign.created_at.desc())
    )).scalars())
    return {"campaigns": [await campaign_stats(session, c) for c in rows]}


@router.get("/api/campaigns/{campaign_id}")
async def get_campaign_route(campaign_id: str, session: AsyncSession = Depends(get_session)):
    from domain.campaigns import campaign_stats

    return await campaign_stats(session, await _campaign_or_404(session, campaign_id))


@router.post("/api/campaigns/{campaign_id}/enqueue")
async def enqueue_campaign(campaign_id: str, request: Request, session: AsyncSession = Depends(get_session)):
    """Queue the leads this campaign may call (authorized, DNC-cleared, in scope)."""
    from domain.campaigns import enqueue_leads, enqueue_sms

    c = await _campaign_or_404(session, campaign_id)
    try:
        body = await request.json()
    except Exception:
        body = {}
    enqueue = enqueue_sms if c.channel_strategy == "sms_first" else enqueue_leads
    result = await enqueue(session, c, limit=(body or {}).get("limit"))
    await session.commit()
    return result


@router.post("/api/campaigns/{campaign_id}/status")
async def campaign_status_route(campaign_id: str, request: Request, session: AsyncSession = Depends(get_session)):
    from domain.campaigns import CampaignError, campaign_stats, set_status

    c = await _campaign_or_404(session, campaign_id)
    body = await request.json()
    try:
        await set_status(session, c, str(body.get("status", "")), body.get("reason"))
    except CampaignError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    return await campaign_stats(session, c)


@router.post("/api/campaigns/{campaign_id}/budget")
async def campaign_budget_route(campaign_id: str, request: Request, session: AsyncSession = Depends(get_session)):
    from domain.campaigns import CampaignError, campaign_stats, raise_budget

    c = await _campaign_or_404(session, campaign_id)
    body = await request.json()
    try:
        await raise_budget(session, c, float(body.get("budget_usd", 0)))
    except (CampaignError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    return await campaign_stats(session, c)


@router.get("/api/reports/operations")
async def operations_report_route(since: str | None = None, until: str | None = None,
                                  campaign_id: str | None = None, session: AsyncSession = Depends(get_session)):
    """Attempts, answered calls, qualified prospects, appointments, opt-outs,
    conversion rates, estimated spend and cost per appointment."""
    from datetime import datetime as _dt, timezone as _tz
    from domain.reporting import operations_report

    def parse(v):
        if not v:
            return None
        try:
            d = _dt.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=422, detail=f"bad timestamp {v!r}") from None
        return d if d.tzinfo else d.replace(tzinfo=_tz.utc)

    return await operations_report(session, tenant_id=pinned_tenant_id(), since=parse(since),
                                   until=parse(until), campaign_id=campaign_id)



@router.get("/api/conversations")
async def list_conversations(status: str | None = None, campaign_id: str | None = None, limit: int = 50,
                             session: AsyncSession = Depends(get_session)):
    from sqlalchemy import select as _select
    from db.models import Lead as _Lead, SmsConversation as _Conv

    q = _select(_Conv, _Lead).join(_Lead, _Lead.id == _Conv.lead_id).where(_Conv.tenant_id == pinned_tenant_id())
    if status:
        q = q.where(_Conv.status == status)
    if campaign_id:
        q = q.where(_Conv.campaign_id == campaign_id)
    rows = (await session.execute(q.order_by(_Conv.updated_at.desc()).limit(max(1, min(limit, 500))))).all()
    return {"conversations": [{
        "id": str(c.id), "lead_id": str(c.lead_id), "name": " ".join(x for x in (l.first_name, l.last_name) if x),
        "status": c.status, "stage": c.stage, "expecting": (c.slots or {}).get("expecting"),
        "inbound": c.inbound_count, "outbound": c.outbound_count, "cost_usd": float(c.cost_usd or 0),
        "next_action": c.next_action, "next_action_at": c.next_action_at.isoformat() if c.next_action_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    } for c, l in rows]}


@router.get("/api/conversations/{conversation_id}")
async def get_conversation(conversation_id: str, session: AsyncSession = Depends(get_session)):
    import uuid as _uuid
    from sqlalchemy import select as _select
    from db.models import Lead as _Lead, QuoteRecord as _Q, SmsConversation as _Conv, SmsMessage as _Msg

    try:
        conv = await session.get(_Conv, _uuid.UUID(conversation_id))
    except ValueError:
        conv = None
    if conv is None or conv.tenant_id != pinned_tenant_id():
        raise HTTPException(status_code=404, detail="conversation not found")
    lead = await session.get(_Lead, conv.lead_id)
    msgs = (await session.execute(_select(_Msg).where(_Msg.conversation_id == conv.id)
                                  .order_by(_Msg.created_at))).scalars()
    quotes = (await session.execute(_select(_Q).where(_Q.conversation_id == conv.id))).scalars()
    return {
        "id": str(conv.id), "status": conv.status, "stage": conv.stage, "slots": conv.slots,
        "lead": {"id": str(lead.id), "name": " ".join(x for x in (lead.first_name, lead.last_name) if x),
                 "phone_e164": lead.phone_e164, "state": lead.state},
        "messages": [{"direction": m.direction, "body": m.body, "at": m.created_at.isoformat() if m.created_at else None,
                      "status": m.status, "segments": m.segments, "intent": m.intent, "quote_id": str(m.quote_id) if m.quote_id else None,
                      "llm_used": m.llm_used} for m in msgs],
        "quotes": [{"quote_id": str(q.id), "quote_ref": q.quote_ref, "carrier": q.carrier, "face": int((q.face_value or 0) / 100),
                    "monthly_premium": str(q.monthly_premium), "source": q.source, "rate_sheet_id": str(q.rate_sheet_id) if q.rate_sheet_id else None,
                    "communicated_at": q.communicated_at.isoformat() if q.communicated_at else None} for q in quotes],
        "appointment_id": str(conv.appointment_id) if conv.appointment_id else None,
        "cost_usd": float(conv.cost_usd or 0),
    }


# ── Upload → preview → import (operator dashboard) ────────────────────────────

_UPLOAD_DIR = "data/private/uploads"
_MAX_UPLOAD_BYTES = 60 * 1024 * 1024
_background: set = set()


def _upload_path(upload_id: str):
    import pathlib
    import re as _re

    if not _re.fullmatch(r"[0-9a-f]{64}", upload_id or ""):
        raise HTTPException(status_code=404, detail="upload not found")
    matches = [m for m in pathlib.Path(_UPLOAD_DIR).glob(f"{upload_id}.*")
               if m.suffix.lower() in (".csv", ".txt", ".xlsx", ".pdf")]   # not the .name sidecar
    if not matches:
        raise HTTPException(status_code=404, detail="upload not found")
    return matches[0]


@router.post("/api/imports/upload")
async def upload_lead_file(request: Request):
    """Store an uploaded CSV / XLSX / PDF (private volume) and preview it."""
    import hashlib
    import pathlib
    from domain.imports.preview import preview_file

    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(status_code=422, detail="multipart field 'file' is required")
    data = await upload.read()
    if not data:
        raise HTTPException(status_code=422, detail="empty file")
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file too large (60 MB max)")
    name = pathlib.Path(str(getattr(upload, "filename", "") or "upload")).name
    ext = (pathlib.Path(name).suffix.lower() or ".csv")[:6]
    if ext not in (".csv", ".txt", ".xlsx", ".pdf"):
        raise HTTPException(status_code=415, detail="upload a .csv, .xlsx or .pdf file")
    digest = hashlib.sha256(data).hexdigest()
    folder = pathlib.Path(_UPLOAD_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{digest}{ext}"
    path.write_bytes(data)
    (folder / f"{digest}.name").write_text(name[:200])
    import asyncio as _asyncio

    preview = await _asyncio.to_thread(preview_file, path)
    return {"upload_id": digest, "file_name": name, "size_bytes": len(data), "preview": preview}


@router.post("/api/imports/uploads/{upload_id}/preview")
async def preview_upload(upload_id: str, request: Request):
    import asyncio as _asyncio
    from domain.imports.preview import preview_file

    body = await request.json()
    path = _upload_path(upload_id)
    return await _asyncio.to_thread(preview_file, path, (body or {}).get("overrides") or {})


@router.post("/api/imports/uploads/{upload_id}/import", status_code=202)
async def import_upload(upload_id: str, request: Request):
    """Start importing an uploaded file in the background (idempotent per file)."""
    import asyncio as _asyncio
    from datetime import date as _date
    from domain.imports import ImportOptions, run_import

    body = await request.json() or {}
    path = _upload_path(upload_id)
    if not str(body.get("source") or "").strip():
        raise HTTPException(status_code=422, detail="source is required")
    try:
        acquired = _date.fromisoformat(body["acquired"]) if body.get("acquired") else None
    except ValueError:
        raise HTTPException(status_code=422, detail="acquired must be YYYY-MM-DD") from None
    label_file = path.with_suffix(".name")
    opts = ImportOptions(source_name=str(body["source"]).strip(), lead_owner=body.get("owner"),
                         acquisition_date=acquired, use_authorization_ref=body.get("use_authorization") or None,
                         column_overrides=body.get("overrides") or {}, tenant_id=pinned_tenant_id(),
                         file_label=label_file.read_text().strip() if label_file.exists() else None)

    async def _go():
        # Import, then the same offline preparation the CLI runs (National DNC
        # scrub against the downloaded registry, then triage and scoring), so
        # an upload from the dashboard is campaign-ready. Contacts no one.
        from domain.lead_ops import qualify_imported, scrub_unknown

        try:
            result = await run_import(path, opts)
            scrub = await scrub_unknown()
            qualify = await qualify_imported()
            logger.info("upload %s imported %s; scrub %s; qualify %s", upload_id[:12],
                        result.as_dict().get("inserted"), scrub.get("by_status"), qualify.get("by_status"))
        except Exception:
            logger.exception("background import of upload %s failed", upload_id[:12])

    task = _asyncio.create_task(_go())
    _background.add(task)
    task.add_done_callback(_background.discard)
    return {"started": True, "upload_id": upload_id, "note": "poll GET /api/imports for progress"}


# ── Dashboard: overview, appointments, outcomes ───────────────────────────────

@router.get("/api/dashboard/overview")
async def dashboard_overview(campaign_id: str | None = None, session: AsyncSession = Depends(get_session)):
    from sqlalchemy import text as _text

    p = {"t": pinned_tenant_id(), "c": campaign_id}
    camp_conv = " AND c.campaign_id = CAST(:c AS uuid)" if campaign_id else ""
    camp = " AND campaign_id = CAST(:c AS uuid)" if campaign_id else ""
    one = lambda sql: session.execute(_text(sql), p)  # noqa: E731
    leads = (await one("SELECT count(*) FROM leads WHERE tenant_id = :t")).scalar_one()
    msgs = (await one(f"""SELECT count(*) FILTER (WHERE m.direction = 'out' AND m.status IN ('sent','delivered')),
                                 count(*) FILTER (WHERE m.direction = 'in'),
                                 coalesce(sum(m.segments) FILTER (WHERE m.direction = 'out' AND m.status IN ('sent','delivered')), 0)
                          FROM sms_messages m JOIN sms_conversations c ON c.id = m.conversation_id
                          WHERE c.tenant_id = :t{camp_conv}""")).one()
    conv = dict((await one(f"""SELECT c.status, count(*) FROM sms_conversations c WHERE c.tenant_id = :t{camp_conv}
                               GROUP BY c.status""")).all())
    replied = (await one(f"SELECT count(*) FROM sms_conversations c WHERE c.tenant_id = :t AND c.inbound_count > 0{camp_conv}")).scalar_one()
    qualified = (await one(f"""SELECT count(*) FROM sms_conversations c WHERE c.tenant_id = :t
                               AND (c.slots ? 'options' OR c.slots ? 'unavailable'){camp_conv}""")).scalar_one()
    quotes = (await one(f"""SELECT count(DISTINCT q.conversation_id) FROM quote_records q
                            JOIN sms_conversations c ON c.id = q.conversation_id WHERE c.tenant_id = :t
                            AND q.communicated_at IS NOT NULL{camp_conv}""")).scalar_one()
    appts = dict((await one(f"""SELECT coalesce(a.outcome, a.status), count(*) FROM appointments a
                                LEFT JOIN sms_conversations c ON c.id = a.conversation_id
                                WHERE a.tenant_id = :t{camp_conv if campaign_id else ''} GROUP BY 1""")).all())
    booked = sum(n for k, n in appts.items() if k not in ("cancelled", "rescheduled"))
    sales = (await one(f"""SELECT count(*), coalesce(sum(annual_premium_usd),0), coalesce(sum(commission_usd),0)
                           FROM policy_sales WHERE tenant_id = :t{camp}""")).one()
    spend = float((await one(f"SELECT coalesce(sum(amount_usd),0) FROM spend_ledger WHERE tenant_id = :t{camp}")).scalar_one())
    attended = appts.get("attended", 0) + appts.get("sold", 0)
    commissions = float(sales[2])
    return {
        "campaign_id": campaign_id,
        "imported_leads": leads,
        "sms_sent": msgs[0], "sms_segments_sent": int(msgs[2]), "replies_received": msgs[1],
        "conversations": conv, "prospects_replied": replied,
        "active_conversations": conv.get("active", 0) + conv.get("contacted", 0),
        "qualified_prospects": qualified, "quotes_communicated": quotes,
        "appointments_booked": booked, "appointments_by_outcome": appts,
        "appointments_attended": attended, "policies_sold": sales[0],
        "annual_premium_written_usd": float(sales[1]), "commissions_usd": commissions,
        "spend_usd": round(spend, 2),
        "cost_per_booked_appointment_usd": round(spend / booked, 2) if booked else None,
        "profit_usd": round(commissions - spend, 2),
    }


@router.get("/api/appointments")
async def list_appointments(upcoming: bool = True, limit: int = 100, session: AsyncSession = Depends(get_session)):
    """Appointments with the full prospect profile the agent needs."""
    from datetime import datetime as _dt, timezone as _tz
    from sqlalchemy import select as _select
    from db.models import Appointment as _A, BookingSlot as _S, HandoffProfile as _H, Lead as _L

    q = (_select(_A, _S, _L).join(_S, _S.id == _A.slot_id).join(_L, _L.id == _A.lead_id)
         .where(_A.tenant_id == pinned_tenant_id()))
    if upcoming:
        q = q.where(_S.starts_at >= _dt.now(_tz.utc), _A.status == "scheduled")
    rows = (await session.execute(q.order_by(_S.starts_at).limit(max(1, min(limit, 500))))).all()
    out = []
    for a, s, lead in rows:
        prof = (await session.execute(_select(_H.profile).where(_H.appointment_id == a.id)
                                      .order_by(_H.created_at.desc()).limit(1))).scalar()
        out.append({"appointment_id": str(a.id), "starts_at": s.starts_at.isoformat(), "status": a.status,
                    "outcome": a.outcome, "lead_id": str(lead.id),
                    "name": " ".join(x for x in (lead.first_name, lead.last_name) if x), "phone_e164": lead.phone_e164,
                    "conversation_id": str(a.conversation_id) if a.conversation_id else None, "profile": prof or {}})
    return {"appointments": out}


@router.post("/api/appointments/{appointment_id}/outcome")
async def appointment_outcome(appointment_id: str, request: Request, session: AsyncSession = Depends(get_session)):
    """Record attendance and sales: attended | no_show | sold (with premium and commission)."""
    import uuid as _uuid
    from db.models import Appointment as _A, PolicySale as _P, SmsConversation as _C

    body = await request.json() or {}
    outcome = str(body.get("outcome") or "")
    if outcome not in ("attended", "no_show", "sold"):
        raise HTTPException(status_code=422, detail="outcome must be attended, no_show or sold")
    try:
        appt = await session.get(_A, _uuid.UUID(appointment_id))
    except ValueError:
        appt = None
    if appt is None or appt.tenant_id != pinned_tenant_id():
        raise HTTPException(status_code=404, detail="appointment not found")
    appt.outcome = outcome
    appt.status = "completed" if outcome != "no_show" else "no_show"
    if outcome == "sold":
        try:
            premium = float(body.get("annual_premium_usd") or 0)
            commission = float(body.get("commission_usd") or 0)
        except ValueError:
            raise HTTPException(status_code=422, detail="premium and commission must be numbers") from None
        campaign_id = None
        if appt.conversation_id:
            conv = await session.get(_C, appt.conversation_id)
            campaign_id = conv.campaign_id if conv else None
        session.add(_P(tenant_id=appt.tenant_id, lead_id=appt.lead_id, appointment_id=appt.id, campaign_id=campaign_id,
                       carrier=body.get("carrier"), product_line=body.get("product_line"),
                       annual_premium_usd=premium, commission_usd=commission, notes=body.get("notes")))
    await session.commit()
    return {"appointment_id": appointment_id, "outcome": outcome}


@router.get("/ops")
async def operator_dashboard():
    """The operator dashboard (static page; every data call needs the API key).
    Served by eastwood-core only — nginx does not expose it."""
    import pathlib
    from starlette.responses import HTMLResponse

    return HTMLResponse((pathlib.Path(__file__).resolve().parent.parent / "static" / "ops.html").read_text())
