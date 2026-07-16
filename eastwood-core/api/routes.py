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


# ── Appointment booking ───────────────────────────────────────────────────────

@router.get("/api/scheduling/availability")
async def scheduling_availability(timezone: str | None = None, limit: int = 3):
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

    if start_time_iso:
        try:
            starts_at = datetime.fromisoformat(start_time_iso.replace("Z", "+00:00"))
            if starts_at.tzinfo is None:
                starts_at = starts_at.replace(tzinfo=timezone.utc)
        except ValueError:
            starts_at = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0) + timedelta(days=1)
    else:
        try:
            starts_at = datetime.fromisoformat(f"{preferred_date}T{preferred_time}").replace(tzinfo=timezone.utc)
        except ValueError:
            starts_at = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0) + timedelta(days=1)

    ends_at = starts_at + timedelta(hours=1)

    slot = _SlotModel(
        tenant_id=settings.default_tenant_id,
        starts_at=starts_at,
        ends_at=ends_at,
        available=False,
    )
    session.add(slot)
    await session.flush()

    appointment = _ApptModel(
        lead_id=_uuid.UUID(lead_id_str),
        slot_id=slot.id,
        tenant_id=settings.default_tenant_id,
        status="scheduled" if calendly_status != "failed" else "requested",
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
        "date": preferred_date or starts_at.date().isoformat(),
        "time": preferred_time if not start_time_iso else starts_at.strftime("%H:%M"),
        "timezone": tz_name,
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
    logger.info("twilio status callback: session=%s status=%s result=%s", session_id, call_status, result)
    return result


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
