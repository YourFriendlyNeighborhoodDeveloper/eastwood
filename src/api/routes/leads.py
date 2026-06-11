from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Any, Dict, Optional
from ...core.clock import now_iso
from ...core.ids import new_id
from ...contracts.events import ConsentEvent
from ...normalize.normalize_lead import normalize
from ...validate.validator import validate_lead
from ...validate.tcpadnc.evidence_pack import build_evidence_pack

class IngestRequest(BaseModel):
    source: str
    payload: Dict[str, Any]

class IngestResponse(BaseModel):
    accepted: bool
    lead_id: str
    booking_token: str
    booking_url: str

def build_router(ctx) -> APIRouter:
    r = APIRouter(prefix="/api/leads", tags=["leads"])

    @r.post("/ingest", response_model=IngestResponse)
    def ingest(req: IngestRequest, request: Request):
        src = str(req.source or "unknown").strip()
        payload = dict(req.payload or {})
        ip = None
        try:
            ip = request.client.host if request.client else None
        except Exception:
            ip = None
        if ip and not payload.get("ip"):
            payload["ip"] = ip

        lead, err = normalize(src, payload)
        if err or not lead:
            raise HTTPException(status_code=400, detail=f"normalize_{err or 'failed'}")

        window_seconds = int(ctx.settings.dedupe_window_days) * 86400
        if ctx.suppression.dedupe_seen_recently(lead["product"], lead["phone_e164"], window_seconds):
            ctx.leads.upsert(lead, status="deduped")
            raise HTTPException(status_code=409, detail="deduped")

        if ctx.suppression.is_suppressed_phone(lead["phone_e164"]):
            ctx.leads.upsert(lead, status="suppressed")
            raise HTTPException(status_code=409, detail="suppressed")

        out, verr = validate_lead(
            lead=lead,
            allowed_products=ctx.settings.allowed_products,
            recency_days=ctx.settings.recency_days,
            consent_required=ctx.settings.consent_required,
            consent_required_fields=ctx.settings.consent_required_fields,
        )
        if verr or not out:
            ctx.leads.upsert(lead, status="rejected")
            raise HTTPException(status_code=400, detail=f"validate_{verr or 'failed'}")

        consent = out.get("consent") or {}
        if consent:
            consent = build_evidence_pack(consent)
            out["consent"] = consent
            consent_id = new_id()
            ctx.consent.insert(consent_id=consent_id, lead_id=out["lead_id"], ts_iso=str(consent.get("ts") or now_iso()), evidence=consent)
            ce = ConsentEvent(event_id=consent_id, ts=now_iso(), lead_id=out["lead_id"], evidence=consent).model_dump()
            ctx.producer.send(ctx.topics.consent_events, ce)

        ctx.leads.upsert(out, status="validated")
        ctx.producer.send(ctx.topics.leads_validated, out)

        tok = ctx.booking.get_or_create(out["lead_id"], ctx.settings.booking_token_ttl_hours)
        booking_url = f"{ctx.settings.booking_public_base_url}/?token={tok['token']}#book"

        return IngestResponse(accepted=True, lead_id=out["lead_id"], booking_token=tok["token"], booking_url=booking_url)

    return r