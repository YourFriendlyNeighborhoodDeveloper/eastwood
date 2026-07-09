"""
Main triage orchestrator.

Pipeline (short-circuits on DEAD):
  L0 Compliance/Contactability gate  → DEAD? stop.
  L1 Universal medical knockout      → DEAD? stop.
  Confidence check                   → low? flag needs_enrichment
  L2 Severity score                  → CALL_NOW | WORKABLE
  Priority sort                      → final queue position
"""
from __future__ import annotations

from datetime import date

from .confidence import compute_confidence
from .gates import check_l0, check_l1
from .models import TriageInput, TriageResult
from .prioritizer import compute_priority
from .ruleset import DEFAULT_RULESET, get_ruleset
from .scorer import compute_severity


def triage(
    inp: TriageInput,
    ruleset: dict | None = None,
    licensed_states: set[str] | None = None,
    agent_has_gi_carrier: bool = True,
) -> TriageResult:
    """
    Score a single lead. Stateless — safe to call concurrently.

    Args:
        inp: Lead data built from the ORM Lead + health_data JSONB.
        ruleset: Active ruleset dict (from DB or DEFAULT_RULESET).
        licensed_states: States the agent is licensed in. None = skip check.
        agent_has_gi_carrier: If False, L1b GI-floor conditions → DEAD.

    Returns:
        TriageResult with bucket, severity, priority, confidence, and drivers.
    """
    rs = get_ruleset(ruleset)
    ls = licensed_states or set()

    # ── Layer 0: compliance & contactability ─────────────────────────────────
    dead = check_l0(inp, rs, ls)
    if dead:
        return dead

    # ── Layer 1: universal medical knockout ──────────────────────────────────
    dead = check_l1(inp, rs)
    if dead:
        return dead

    # ── Confidence / data completeness ───────────────────────────────────────
    confidence = compute_confidence(inp, rs)
    needs_enrichment = confidence < 0.50

    # ── Layer 2: severity scoring ─────────────────────────────────────────────
    severity, drivers = compute_severity(inp, rs)

    if not drivers:
        drivers = ["no significant health factors reported — verify live"]

    # ── Bucket assignment ─────────────────────────────────────────────────────
    thresholds = rs.get("thresholds", {})
    T1 = float(thresholds.get("T1_call_now_ceiling", thresholds.get("T1", 0.35)))
    T2 = float(thresholds.get("T2_workable_ceiling", thresholds.get("T2", 0.85)))

    # GI-floor: severity is pushed to T2 ceiling by scorer; kill here if no carrier
    if severity >= T2 and not agent_has_gi_carrier:
        return TriageResult(
            lead_id=inp.lead_id,
            bucket="DEAD",
            severity_score=round(severity, 4),
            priority=0.0,
            confidence=confidence,
            needs_enrichment=False,
            drivers=[f"GI-floor severity {severity:.2f} >= T2={T2} — no GI carrier on shelf"] + drivers,
            dead_reason="gi_floor_no_carrier",
            ruleset_version=rs["ruleset_version"],
            scored_at=date.today(),
        )

    if severity < T1:
        bucket = "CALL_NOW"
        drivers.insert(0, f"CALL_NOW: severity {severity:.2f} < T1={T1} — level-likely")
    else:
        bucket = "WORKABLE"
        drivers.insert(0, f"WORKABLE: severity {severity:.2f} — graded/GI-likely")

    if needs_enrichment:
        drivers.append(f"low confidence ({confidence:.2f}) — route to voice agent for health capture")

    # ── Priority within bucket ────────────────────────────────────────────────
    priority = compute_priority(inp, severity, rs)

    return TriageResult(
        lead_id=inp.lead_id,
        bucket=bucket,
        severity_score=round(severity, 4),
        priority=priority,
        confidence=confidence,
        needs_enrichment=needs_enrichment,
        drivers=drivers,
        dead_reason=None,
        ruleset_version=rs["ruleset_version"],
        scored_at=date.today(),
    )


def triage_from_lead_dict(lead: dict, health_data: dict | None = None, ruleset: dict | None = None) -> TriageResult:
    """
    Convenience builder: construct TriageInput from the flat lead dict
    (as stored in raw_payload / normalized) + optional health_data JSONB.
    """
    from .models import Compliance, Condition, Medication, TobaccoStatus

    hd = health_data or {}

    conditions = [Condition(**(c if isinstance(c, dict) else {"code": c})) for c in hd.get("conditions", [])]
    medications = [Medication(**(m if isinstance(m, dict) else {"code": m})) for m in hd.get("medications", [])]

    compliance = Compliance(
        on_dnc=lead.get("on_dnc"),
        has_express_written_consent=bool(lead.get("express_written_consent")),
        known_litigator=bool(hd.get("known_litigator", False)),
        phone_valid=hd.get("phone_valid"),
        phone_type=hd.get("phone_type"),
        is_the_insured=hd.get("is_the_insured"),
    )

    tobacco_raw = str(hd.get("tobacco", "unknown")).lower()
    try:
        tobacco = TobaccoStatus(tobacco_raw)
    except ValueError:
        tobacco = TobaccoStatus.UNKNOWN

    inp = TriageInput(
        lead_id=str(lead.get("lead_id", "")),
        source=str(lead.get("source", "unknown")),
        state=lead.get("state"),
        age=lead.get("age"),
        tobacco=tobacco,
        conditions=conditions,
        medications=medications,
        compliance=compliance,
        enrichment=lead.get("enrichment") or {},
    )
    return triage(inp, ruleset)
