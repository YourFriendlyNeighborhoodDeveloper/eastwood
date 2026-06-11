"""
Priority sort within bucket.

Bucket decides whether/when to dial; priority decides the ORDER inside the bucket.
The power dialer pulls CALL_NOW leads sorted descending by priority.
"""
from __future__ import annotations

import math
from datetime import date

from .models import TriageInput


def _freshness(generated_on: date | None, halflife_days: float) -> float:
    """Exponential decay. Speed-to-lead is the biggest conversion lever."""
    if generated_on is None:
        return 0.50
    days = max((date.today() - generated_on).days, 0)
    return math.exp(-days * math.log(2) / halflife_days)


def _intent(source: str, source_intent_map: dict) -> float:
    source_lower = source.lower()
    # Try exact match, then prefix match
    if source_lower in source_intent_map:
        return float(source_intent_map[source_lower])
    for key, val in source_intent_map.items():
        if source_lower.startswith(key) or key in source_lower:
            return float(val)
    return float(source_intent_map.get("default", 0.50))


def _attempt_penalty(contact_attempts: int, penalty_per_try: float, penalty_cap: float) -> float:
    """Leads dialed many times with no answer sink, they don't vanish."""
    return max(1.0 - penalty_cap, 1.0 - penalty_per_try * contact_attempts)


def _replacement_value(inp: TriageInput, contestability_years: float) -> float:
    """
    Orphan/overpay angle. Hard-zeroed when contestability guard trips
    (existing policy < contestability_years old).
    """
    if not inp.current_policy:
        return 0.0

    policy = inp.current_policy

    if policy.issue_date:
        years_old = (date.today() - policy.issue_date).days / 365.25
        if years_old < contestability_years:
            return 0.0  # replacement suppressed — reset would harm client

    orphan_score = 0.5 if policy.carrier else 0.0
    return orphan_score


def compute_priority(inp: TriageInput, severity: float, ruleset: dict) -> float:
    """
    Returns priority ∈ [0, 1] — higher = dial sooner.
    """
    w = ruleset.get("priority_weights", {})
    # Accept both spec names and legacy w1-w5 keys
    w1 = float(w.get("w_cleanliness",  w.get("w1", 0.30)))
    w2 = float(w.get("w_freshness",    w.get("w2", 0.30)))
    w3 = float(w.get("w_intent",       w.get("w3", 0.15)))
    w4 = float(w.get("w_attempts",     w.get("w4", 0.10)))
    w5 = float(w.get("w_replacement",  w.get("w5", 0.15)))

    halflife         = float(ruleset.get("freshness_halflife_days", 30))
    source_map       = ruleset.get("source_intent", {})
    penalty_per_try  = float(ruleset.get("attempt_penalty_per_try", 0.12))
    penalty_cap      = float(ruleset.get("attempt_penalty_cap", 0.90))
    # Accept both spec name (years) and legacy name (months)
    contestability_months = ruleset.get("replacement_contestability_months")
    if contestability_months is not None:
        contestability_years = float(contestability_months) / 12.0
    else:
        contestability_years = float(ruleset.get("contestability_window_years", 2))

    cleanliness = 1.0 - severity
    freshness   = _freshness(inp.generated_on, halflife)
    intent      = _intent(inp.source, source_map)
    attempt_pen = _attempt_penalty(inp.contact_attempts, penalty_per_try, penalty_cap)
    replacement = _replacement_value(inp, contestability_years)

    priority = (
        w1 * cleanliness
        + w2 * freshness
        + w3 * intent
        + w4 * attempt_pen
        + w5 * replacement
    )
    return round(max(0.0, min(priority, 1.0)), 4)
