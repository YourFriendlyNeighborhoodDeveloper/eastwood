"""
Confidence = how much we trust this triage score, based on data completeness.

confidence < 0.5 → needs_enrichment = True → route to voice agent before final ranking.
This is the explicit "I don't know — verify" output. A confident-wrong score is worse
than no score.
"""
from __future__ import annotations

import math
from datetime import date

from .models import TriageInput


def _recency_factor(generated_on: date | None, halflife_days: int = 180) -> float:
    """Confidence degrades with lead age (stale data = stale score)."""
    if generated_on is None:
        return 0.80  # unknown age — assume reasonably fresh
    days_old = (date.today() - generated_on).days
    return math.exp(-days_old * math.log(2) / halflife_days)


def compute_confidence(inp: TriageInput, ruleset: dict | None = None) -> float:
    """Returns confidence ∈ [0, 1]."""
    halflife = int((ruleset or {}).get("freshness_halflife_days", 180))

    # Decision-critical fields (spec: 5 required for decisive score)
    present = 0
    total = 5

    if inp.age or inp.dob:
        present += 1

    if inp.state:
        present += 1

    if inp.tobacco.value != "unknown":
        present += 1

    if inp.conditions or inp.medications:
        present += 1

    if inp.compliance.on_dnc is not None:
        present += 1

    completeness = present / total
    recency = _recency_factor(inp.generated_on, halflife)

    return round(completeness * recency, 3)
