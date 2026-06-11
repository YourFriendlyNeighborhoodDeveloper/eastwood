"""
L0 — Compliance & contactability gate (deterministic DEAD).
L1 — Universal medical knockout (near-certain uninsurable).

Short-circuit: the moment either layer returns a result, the engine stops.
Medical scoring (expensive, fuzzy) only runs on leads that survive both gates.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from .models import TriageInput, TriageResult


def _dead(inp: TriageInput, reason: str, drivers: list[str], ruleset: dict) -> TriageResult:
    return TriageResult(
        lead_id=inp.lead_id,
        bucket="DEAD",
        severity_score=1.0,
        priority=0.0,
        confidence=1.0,
        needs_enrichment=False,
        drivers=drivers,
        dead_reason=reason,
        ruleset_version=ruleset["ruleset_version"],
        scored_at=date.today(),
    )


def _age_from_dob(dob: date) -> int:
    today = date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


# ── Layer 0 ───────────────────────────────────────────────────────────────────

def check_l0(
    inp: TriageInput,
    ruleset: dict,
    licensed_states: set[str],
) -> Optional[TriageResult]:
    """Compliance and contactability gate. Returns DEAD if triggered, else None."""
    c = inp.compliance

    if c.on_dnc and not c.has_express_written_consent:
        return _dead(inp, "dnc_no_consent",
                     ["DNC list: no express written consent on file"], ruleset)

    if c.known_litigator:
        return _dead(inp, "litigator_quarantine",
                     ["Known TCPA litigator — quarantined, not deleted"], ruleset)

    if c.phone_valid is False:
        return _dead(inp, "no_contact",
                     ["Phone number invalid or missing"], ruleset)

    if c.is_the_insured is False:
        return _dead(inp, "wrong_party",
                     ["Contact confirmed not to be the insured"], ruleset)

    if inp.state and licensed_states and inp.state.upper() not in licensed_states:
        return _dead(inp, "unlicensed_state",
                     [f"State {inp.state} not in agent licensed states"], ruleset)

    age = inp.age or (_age_from_dob(inp.dob) if inp.dob else None)
    if age is not None:
        max_age = ruleset.get("max_issue_age", 85)
        min_age = ruleset.get("min_issue_age", 18)
        if age > max_age:
            return _dead(inp, "age_out",
                         [f"Age {age} exceeds max issue age {max_age}"], ruleset)
        if age < min_age:
            return _dead(inp, "age_out",
                         [f"Age {age} below min issue age {min_age}"], ruleset)

    return None


# ── Layer 1 ───────────────────────────────────────────────────────────────────

def check_l1(inp: TriageInput, ruleset: dict) -> Optional[TriageResult]:
    """
    Universal medical knockout. Returns DEAD only for hard_universal_decline conditions.
    GI-floor conditions are handled in L2 (severity pushed to T2 ceiling) and bucket
    assignment (engine kills if severity >= T2 and no GI carrier).
    """
    condition_codes = {c.code for c in inp.conditions}
    med_codes = {m.code for m in inp.medications}

    hard_knockouts: list[str] = ruleset.get("hard_universal_decline", [])

    # Dementia meds escalate to L1 review
    dementia_meds = {
        p["code"]
        for p in ruleset.get("med_proxies", [])
        if p.get("escalate_to_l1") and p["code"] in med_codes
    }
    if dementia_meds:
        condition_codes.add("dementia_alzheimers")

    # Hard universal decline — nobody writes this
    triggered = condition_codes & set(hard_knockouts)
    if triggered:
        code = next(iter(triggered))
        return _dead(inp, "l1_hard_knockout",
                     [f"L1 universal knockout: {code} — no product writes this"], ruleset)

    return None
