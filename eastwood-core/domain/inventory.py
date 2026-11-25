"""Lead inventory: what is in the database, what may be contacted, and what
FTC registry coverage costs.

Every number here is computed from the leads table; nothing is estimated
except the registry fee schedule, which is configurable (FY2027 defaults:
$85 per area code per year after the first five free, $23,425 nationwide cap).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text

from config.settings import get_settings

FTC_FEE_PER_AREA_CODE_USD = 85.0
FTC_FREE_AREA_CODES = 5
FTC_NATIONWIDE_CAP_USD = 23_425.0


def ftc_registry_cost(area_codes: int) -> float:
    """Annual National DNC Registry access fee for this many area codes."""
    return min(max(area_codes - FTC_FREE_AREA_CODES, 0) * FTC_FEE_PER_AREA_CODE_USD, FTC_NATIONWIDE_CAP_USD)


_AREA = "substring(phone_e164 from 3 for 3)"
_AI = "coalesce((contact_authorization->>'ai_voice')::boolean, false)"
_SMS = "coalesce((contact_authorization->>'sms')::boolean, false)"
_MANUAL = "coalesce((contact_authorization->>'manual_call')::boolean, false)"
_AGE_BUCKET = """
  CASE
    WHEN acquired_at IS NULL THEN 'unknown'
    WHEN acquired_at > CAST(:now AS timestamptz) - interval '30 days' THEN '0-30 days'
    WHEN acquired_at > CAST(:now AS timestamptz) - interval '90 days' THEN '31-90 days'
    WHEN acquired_at > CAST(:now AS timestamptz) - interval '180 days' THEN '91-180 days'
    WHEN acquired_at > CAST(:now AS timestamptz) - interval '365 days' THEN '181-365 days'
    WHEN acquired_at > CAST(:now AS timestamptz) - interval '730 days' THEN '1-2 years'
    ELSE 'over 2 years'
  END"""
_AGE_ORDER = ["0-30 days", "31-90 days", "91-180 days", "181-365 days", "1-2 years", "over 2 years", "unknown"]


async def _rows(session, sql: str, params: dict) -> list[dict]:
    result = await session.execute(text(sql), params)
    return [dict(r._mapping) for r in result]


async def build_inventory(session, tenant_id: str | None = None, *, now: datetime | None = None,
                          import_batch_id: str | None = None) -> dict:
    settings = get_settings()
    tenant_id = tenant_id or settings.default_tenant_id
    now = now or datetime.now(timezone.utc)
    active = settings.active_states_list()
    where = "tenant_id = :t" + (" AND import_batch_id = CAST(:b AS uuid)" if import_batch_id else "")
    p = {"t": tenant_id, "now": now, "b": import_batch_id, "active": active}

    def grouped(expr: str, label: str, order: str = "leads DESC") -> str:
        return (f"SELECT {expr} AS {label}, count(*) AS leads, "
                f"count(*) FILTER (WHERE {_AI}) AS ai_voice_eligible "
                f"FROM leads WHERE {where} GROUP BY 1 ORDER BY {order}")

    totals = (await _rows(session, f"""
        SELECT count(*) AS leads,
               count(*) FILTER (WHERE {_AI}) AS ai_voice_eligible,
               count(*) FILTER (WHERE {_SMS}) AS sms_eligible,
               count(*) FILTER (WHERE {_MANUAL} AND NOT {_AI}) AS manual_review_only,
               count(*) FILTER (WHERE status = 'quarantined') AS quarantined,
               count(*) FILTER (WHERE {_AI} AND national_dnc_status IN ('clear','exempt')) AS ai_voice_dnc_cleared,
               count(*) FILTER (WHERE {_AI} AND state = ANY(:active)) AS ai_voice_in_active_states,
               count(DISTINCT {_AREA}) AS area_codes,
               count(DISTINCT {_AREA}) FILTER (WHERE {_AI}) AS area_codes_ai_voice,
               count(DISTINCT {_AREA}) FILTER (WHERE {_AI} AND state = ANY(:active)) AS area_codes_ai_voice_active
        FROM leads WHERE {where}""", p))[0]

    by_state = await _rows(session, grouped("coalesce(state, 'unknown')", "state"), p)
    by_area = await _rows(session, grouped(_AREA, "area_code"), p)
    by_product = await _rows(session, grouped("coalesce(product_line, 'unspecified')", "product_line"), p)
    by_age = await _rows(session, grouped(_AGE_BUCKET, "lead_age"), p)
    by_age.sort(key=lambda r: _AGE_ORDER.index(r["lead_age"]))
    by_consent = await _rows(session, grouped(
        "coalesce(contact_authorization->>'consent_status', 'not_evaluated')", "consent_status"), p)
    by_dnc = await _rows(session, grouped("national_dnc_status", "national_dnc_status"), p)
    by_status = await _rows(session, grouped("status", "status"), p)
    eligibility = await _rows(session, f"""
        SELECT CASE WHEN {_AI} AND {_SMS} THEN 'ai_voice_and_sms'
                    WHEN {_AI} THEN 'ai_voice_only'
                    WHEN {_SMS} THEN 'sms_only'
                    WHEN {_MANUAL} THEN 'manual_review_only'
                    ELSE 'no_contact' END AS eligibility, count(*) AS leads
        FROM leads WHERE {where} GROUP BY 1 ORDER BY 2 DESC""", p)
    reasons = await _rows(session, f"""
        SELECT r AS reason, count(*) AS leads
        FROM leads, jsonb_array_elements_text(coalesce(quarantine_reasons, '[]'::jsonb)) r
        WHERE {where} GROUP BY 1 ORDER BY 2 DESC""", p)

    def coverage(rows: list[dict], key: str) -> list[dict]:
        ordered = sorted((r for r in rows if r[key] > 0), key=lambda r: -r[key])
        total = sum(r[key] for r in ordered)
        points = sorted({n for n in (5, 10, 15, 20, 30, 50, 75, 100) if n < len(ordered)} | {len(ordered)})
        out = []
        for n in points:
            covered = sum(r[key] for r in ordered[:n])
            out.append({"area_codes": n, "leads_covered": covered,
                        "share": round(covered / total, 4) if total else 0.0,
                        "annual_fee_usd": ftc_registry_cost(n)})
        return out

    ftc = {
        "fee_schedule": {"per_area_code_usd": FTC_FEE_PER_AREA_CODE_USD, "free_area_codes": FTC_FREE_AREA_CODES,
                         "nationwide_cap_usd": FTC_NATIONWIDE_CAP_USD, "fiscal_year": "FY2027 (Oct 2026-Sep 2027)"},
        "all_leads": {"area_codes": totals["area_codes"], "annual_fee_usd": ftc_registry_cost(totals["area_codes"])},
        "ai_voice_eligible": {"area_codes": totals["area_codes_ai_voice"],
                              "annual_fee_usd": ftc_registry_cost(totals["area_codes_ai_voice"])},
        "ai_voice_eligible_active_states": {"area_codes": totals["area_codes_ai_voice_active"],
                                            "annual_fee_usd": ftc_registry_cost(totals["area_codes_ai_voice_active"])},
        "coverage_curve_all_leads": coverage(by_area, "leads"),
        "coverage_curve_ai_voice": coverage(by_area, "ai_voice_eligible"),
        "note": ("Billed per phone area code, not per state. Numbers in unsubscribed area codes "
                 "stay 'unknown' and are never dialed."),
    }
    return {
        "generated_at": now.isoformat(),
        "tenant_id": tenant_id,
        "import_batch_id": import_batch_id,
        "active_states": active,
        "totals": totals,
        "contact_eligibility": eligibility,
        "quarantine_reasons": reasons,
        "by_state": by_state,
        "by_area_code": by_area,
        "by_product_interest": by_product,
        "by_lead_age": by_age,
        "by_consent_status": by_consent,
        "by_national_dnc_status": by_dnc,
        "by_status": by_status,
        "ftc_registry": ftc,
    }


def inventory_markdown(inv: dict, top: int = 15) -> str:
    t = inv["totals"]
    lines = [
        f"# Lead inventory — {inv['generated_at'][:19]}Z",
        "",
        f"- Leads: **{t['leads']:,}**  ·  AI-voice eligible: **{t['ai_voice_eligible']:,}**  ·  "
        f"SMS eligible: {t['sms_eligible']:,}  ·  manual review only: {t['manual_review_only']:,}  ·  "
        f"quarantined: {t['quarantined']:,}",
        f"- AI-voice eligible in ACTIVE_STATES ({', '.join(inv['active_states'])}): {t['ai_voice_in_active_states']:,}"
        f"  ·  DNC-cleared: {t['ai_voice_dnc_cleared']:,}",
        "",
        "## FTC registry cost (annual)",
        "",
        "| Scope | Area codes | Fee |",
        "| --- | ---: | ---: |",
    ]
    f = inv["ftc_registry"]
    for key, label in (("all_leads", "All leads"), ("ai_voice_eligible", "AI-voice eligible"),
                       ("ai_voice_eligible_active_states", "AI-voice eligible, active states")):
        lines.append(f"| {label} | {f[key]['area_codes']} | ${f[key]['annual_fee_usd']:,.0f} |")
    lines += ["", "Coverage curve (all leads, busiest area codes first):", "",
              "| Area codes | Leads covered | Share | Fee |", "| ---: | ---: | ---: | ---: |"]
    for pt in f["coverage_curve_all_leads"]:
        lines.append(f"| {pt['area_codes']} | {pt['leads_covered']:,} | {pt['share']:.0%} | ${pt['annual_fee_usd']:,.0f} |")

    def table(title: str, rows: list[dict], key: str, limit: int | None = None) -> None:
        lines.extend(["", f"## {title}", "", f"| {key} | Leads | AI-voice eligible |", "| --- | ---: | ---: |"])
        for r in rows[: limit or len(rows)]:
            lines.append(f"| {r[key]} | {r['leads']:,} | {r.get('ai_voice_eligible', 0):,} |")
        if limit and len(rows) > limit:
            lines.append(f"| … {len(rows) - limit} more | | |")

    lines.extend(["", "## Contact eligibility", "", "| Eligibility | Leads |", "| --- | ---: |"])
    for r in inv["contact_eligibility"]:
        lines.append(f"| {r['eligibility']} | {r['leads']:,} |")
    lines.extend(["", "## Quarantine reasons (a lead can have several)", "", "| Reason | Leads |", "| --- | ---: |"])
    for r in inv["quarantine_reasons"]:
        lines.append(f"| {r['reason']} | {r['leads']:,} |")
    table("By state", inv["by_state"], "state", top)
    table("By area code", inv["by_area_code"], "area_code", top)
    table("By product interest", inv["by_product_interest"], "product_line")
    table("By lead age", inv["by_lead_age"], "lead_age")
    table("By consent status", inv["by_consent_status"], "consent_status")
    table("By National DNC status", inv["by_national_dnc_status"], "national_dnc_status")
    return "\n".join(lines) + "\n"
