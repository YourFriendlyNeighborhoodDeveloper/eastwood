"""Bulk lead operations for imported inventory: National DNC scrub and qualify.

Both are local and offline: the scrub uses the configured provider (the
downloaded FTC dataset in production) and qualification runs triage + scoring
in-process with NO third-party enrichment. Neither contacts anyone.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select, update

from config.settings import get_settings
from db.models import Lead
from db.session import session_scope

logger = logging.getLogger(__name__)


async def scrub_unknown(*, chunk: int = 1000, limit: int | None = None) -> dict:
    """Resolve national_dnc_status for leads still 'unknown'."""
    from domain.compliance import get_scrub_provider, scrub_number

    s = get_settings()
    provider = get_scrub_provider(s)
    started, counts, done = time.perf_counter(), {}, 0
    last_id = None
    while True:
        async with session_scope() as db:
            q = select(Lead.id, Lead.phone_e164).where(
                Lead.tenant_id == s.default_tenant_id, Lead.national_dnc_status == "unknown"
            ).order_by(Lead.id).limit(chunk)
            if last_id is not None:
                q = q.where(Lead.id > last_id)
            rows = (await db.execute(q)).all()
            if not rows:
                break
            for lead_id, phone in rows:
                try:
                    r = await scrub_number(phone, provider=provider, settings=s)
                except Exception:
                    logger.exception("scrub failed for lead %s — stays unknown", lead_id)
                    continue
                counts[r.status] = counts.get(r.status, 0) + 1
                if r.status != "unknown":
                    await db.execute(update(Lead).where(Lead.id == lead_id).values(
                        national_dnc_status=r.status, national_dnc_provider=r.provider,
                        national_dnc_checked_at=r.checked_at, national_dnc_source=r.source,
                        national_dnc_audit=getattr(r, "audit", None),
                        status="compliance_blocked" if r.status == "blocked" else Lead.status,
                    ))
            last_id = rows[-1][0]
            done += len(rows)
        if limit and done >= limit:
            break
    elapsed = time.perf_counter() - started
    return {"scrubbed": done, "by_status": counts, "provider": s.dnc_scrub_provider,
            "elapsed_seconds": round(elapsed, 2), "per_second": round(done / elapsed, 1) if elapsed else None}


async def qualify_imported(*, chunk: int = 1000) -> dict:
    """Triage + score every 'imported' (authorized) lead. DEAD → status dead."""
    from repos.repos import RulesetRepo
    from domain.scorer import score_lead
    from domain.triage import triage_from_lead_dict

    s = get_settings()
    started, counts, done = time.perf_counter(), {}, 0
    async with session_scope() as db:
        ruleset = await RulesetRepo(db).get_active()
        payload = ruleset.payload if ruleset else None
    now = datetime.now(timezone.utc)
    while True:
        async with session_scope() as db:
            leads = list((await db.execute(
                select(Lead).where(Lead.tenant_id == s.default_tenant_id, Lead.status == "imported").limit(chunk)
            )).scalars())
            if not leads:
                break
            for lead in leads:
                auth = lead.contact_authorization or {}
                triage = triage_from_lead_dict(
                    lead={"lead_id": str(lead.id), "source": lead.source or "import", "state": lead.state,
                          "age": lead.age, "on_dnc": False,
                          "express_written_consent": auth.get("consent_status") == "documented"},
                    health_data=lead.health_data or {}, ruleset=payload,
                )
                score = score_lead(
                    express_written=auth.get("consent_status") == "documented", consent_age_days=None,
                    product_line=lead.product_line, age=lead.age, state=lead.state, recent_touches=0,
                    enrichment=lead.enrichment or {}, active_states=s.active_states_list(),
                )
                status = "dead" if triage.bucket == "DEAD" else "qualified"
                counts[status] = counts.get(status, 0) + 1
                await db.execute(update(Lead).where(Lead.id == lead.id).values(
                    status=status, score=score.score, score_band=score.band, score_components=score.components,
                    triage_bucket=triage.bucket, triage_priority=triage.priority, updated_at=now,
                ))
            done += len(leads)
    elapsed = time.perf_counter() - started
    return {"qualified": done, "by_status": counts, "elapsed_seconds": round(elapsed, 2),
            "per_second": round(done / elapsed, 1) if elapsed else None}
