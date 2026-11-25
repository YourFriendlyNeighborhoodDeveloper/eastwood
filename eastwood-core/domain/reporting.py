"""Operational reporting: the outbound funnel, opt-outs, spend and unit costs.

Facts come from dial_attempts (one row per call initiated), quote_records,
appointments, suppressions and spend_ledger. With a campaign filter, quotes and
appointments count only for leads that campaign actually dialed.

  answered      completed call, not answered by a machine
  qualified     distinct leads that were presented a quote
  appointments  appointments created (scheduled or requested)
  opt-outs      suppressions created (by reason)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from config.settings import get_settings


def _rate(n: float, d: float) -> float | None:
    return round(n / d, 4) if d else None


def _per(cost: float, n: float) -> float | None:
    return round(cost / n, 2) if n else None


async def operations_report(session, *, tenant_id: str | None = None, since: datetime | None = None,
                            until: datetime | None = None, campaign_id: str | None = None) -> dict:
    s = get_settings()
    tenant_id = tenant_id or s.default_tenant_id
    until = until or datetime.now(timezone.utc)
    since = since or (until - timedelta(days=30))
    p = {"t": tenant_id, "since": since, "until": until, "c": campaign_id}
    camp = " AND campaign_id = CAST(:c AS uuid)" if campaign_id else ""
    lead_scope = (" AND lead_id IN (SELECT lead_id FROM dial_attempts WHERE campaign_id = CAST(:c AS uuid))"
                  if campaign_id else "")
    quote_scope = (" AND q.lead_id IN (SELECT da.lead_id FROM dial_attempts da WHERE da.campaign_id = CAST(:c AS uuid))"
                   if campaign_id else "")

    a = (await session.execute(text(f"""
        SELECT count(*) AS attempts,
               count(*) FILTER (WHERE status = 'completed' AND coalesce(answered_by,'') NOT LIKE 'machine%'
                                AND coalesce(duration_seconds,0) > 0) AS answered,
               count(*) FILTER (WHERE status = 'completed' AND answered_by LIKE 'machine%') AS machine,
               count(*) FILTER (WHERE status = 'no_answer') AS no_answer,
               count(*) FILTER (WHERE status = 'busy') AS busy,
               count(*) FILTER (WHERE status = 'failed') AS failed,
               count(*) FILTER (WHERE status = 'reaped') AS lost_callbacks,
               count(*) FILTER (WHERE ended_at IS NULL) AS in_flight,
               count(DISTINCT lead_id) AS leads_dialed,
               coalesce(sum(duration_seconds), 0) AS talk_seconds
        FROM dial_attempts
        WHERE tenant_id = :t AND started_at >= :since AND started_at < :until{camp}
    """), p)).mappings().one()

    qualified = (await session.execute(text(f"""
        SELECT count(DISTINCT q.lead_id) FROM quote_records q JOIN leads l ON l.id = q.lead_id
        WHERE l.tenant_id = :t AND q.presented_at >= :since AND q.presented_at < :until
              {quote_scope}
    """), p)).scalar_one()

    appts = (await session.execute(text(f"""
        SELECT status, count(*) FROM appointments
        WHERE tenant_id = :t AND created_at >= :since AND created_at < :until{lead_scope}
        GROUP BY status
    """), p)).all()
    appointments = {st: n for st, n in appts}
    appointment_total = sum(appointments.values())

    optouts = {r: n for r, n in (await session.execute(text(f"""
        SELECT reason, count(*) FROM suppressions
        WHERE tenant_id = :t AND created_at >= :since AND created_at < :until
              {"AND phone_e164 IN (SELECT l.phone_e164 FROM leads l JOIN dial_attempts d ON d.lead_id = l.id WHERE d.campaign_id = CAST(:c AS uuid))" if campaign_id else ""}
        GROUP BY reason
    """), p)).all()}
    prospect_optouts = optouts.get("dnc_request", 0) + optouts.get("sms_opt_out", 0)

    spend_rows = (await session.execute(text(f"""
        SELECT kind, coalesce(sum(amount_usd), 0) FROM spend_ledger
        WHERE tenant_id = :t AND created_at >= :since AND created_at < :until{camp}
        GROUP BY kind
    """), p)).all()
    spend = {k: round(float(v), 2) for k, v in spend_rows}
    spend_total = round(sum(spend.values()), 2)

    answered = a["answered"]
    return {
        "window": {"since": since.isoformat(), "until": until.isoformat()},
        "campaign_id": campaign_id,
        "funnel": {
            "attempts": a["attempts"], "leads_dialed": a["leads_dialed"], "answered": answered,
            "voicemail_or_machine": a["machine"], "no_answer": a["no_answer"], "busy": a["busy"],
            "failed": a["failed"], "lost_callbacks": a["lost_callbacks"], "in_flight": a["in_flight"],
            "qualified_prospects": qualified, "appointments": appointment_total,
            "appointments_by_status": appointments,
            "talk_minutes": round(a["talk_seconds"] / 60, 1),
        },
        "opt_outs": {"total_prospect_opt_outs": prospect_optouts, "by_reason": optouts},
        "rates": {
            "answer_rate": _rate(answered, a["attempts"]),
            "qualification_rate_of_answered": _rate(qualified, answered),
            "appointment_rate_of_answered": _rate(appointment_total, answered),
            "appointment_rate_of_qualified": _rate(appointment_total, qualified),
            "appointments_per_100_attempts": round(100 * appointment_total / a["attempts"], 2) if a["attempts"] else None,
            "opt_out_rate_of_answered": _rate(prospect_optouts, answered),
        },
        "spend": {"total_usd": spend_total, "by_kind": spend,
                  "note": "estimated from COST_* unit prices; reconcile with provider invoices"},
        "unit_costs": {
            "per_attempt_usd": _per(spend_total, a["attempts"]),
            "per_answered_call_usd": _per(spend_total, answered),
            "per_qualified_prospect_usd": _per(spend_total, qualified),
            "per_appointment_usd": _per(spend_total, appointment_total),
        },
    }
