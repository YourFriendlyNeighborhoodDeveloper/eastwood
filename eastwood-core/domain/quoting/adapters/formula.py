from __future__ import annotations

import logging
import math

from ..base import CarrierQuote, CarrierQuoteAdapter, QuoteRequest

logger = logging.getLogger(__name__)

# Base rate per $1,000 face value at age 50, non-tobacco, level tier.
# Multiplied by age_mult, tob_mult, and tier_mult to produce the monthly premium.
# Calibrated against mid-market FE benchmarks (Mutual of Omaha Living Promise,
# Royal Neighbors WL, Prosperity Life, Transamerica, American Amicable).
_CARRIER_RATES: dict[str, dict] = {
    "American Amicable": {"base_per_k": 2.40, "level": 1.00, "graded": 1.45, "modified": 1.65, "gi": 1.90},
    "Mutual of Omaha":   {"base_per_k": 2.55, "level": 1.00, "graded": 1.45, "modified": 1.65, "gi": 1.90},
    "Transamerica":      {"base_per_k": 2.35, "level": 1.00, "graded": 1.50, "modified": 1.70, "gi": 1.95},
    "Prosperity Life":   {"base_per_k": 2.45, "level": 1.00, "graded": 1.44, "modified": 1.63, "gi": 1.88},
    "Royal Neighbors":   {"base_per_k": 2.60, "level": 1.00, "graded": 1.46, "modified": 1.66, "gi": 1.92},
}

_TOBACCO_MULT = 1.35


def _age_mult(age: int) -> float:
    """
    Compound age multiplier fitted to real-world FE benchmarks.
    Uses exponential-quadratic form: exp(k1*x + k2*x^2) where x = age - 50.
    Reference points (non-tobacco, level, mid-market):
      age 50 → 1.00x  (base)
      age 60 → 1.60x  → $40/mo at $10k
      age 65 → 2.16x  → $55/mo at $10k
      age 68 → 2.64x  → $165/mo at $25k (Mutual of Omaha)
      age 70 → 3.05x  → $80/mo at $10k
      age 75 → 4.51x  → $115/mo at $10k
      age 80 → 6.97x  → $175/mo at $10k
    """
    x = max(0, age - 50)
    return math.exp(0.038 * x + 0.00089 * x * x)


def _fe_premium(age: int, face_value_dollars: float, tobacco: bool, tier: str, cal: dict) -> float:
    tier_mult = cal.get(tier, 1.0)
    tob_mult = _TOBACCO_MULT if tobacco else 1.0
    premium = (face_value_dollars / 1000) * cal["base_per_k"] * _age_mult(age) * tob_mult * tier_mult
    return round(max(premium, 9.99), 2)


class FormulaAdapter(CarrierQuoteAdapter):
    name = "formula"
    supported_products = ["final_expense", "life"]

    async def get_quotes(self, request: QuoteRequest) -> list[CarrierQuote]:
        if request.age is None:
            return []

        # face_value arrives in cents; convert to dollars for the per-$1k rate
        face_value_dollars = (request.face_value or 1_000_000) / 100.0
        tier = request.rate_tier or "level"

        quotes = []
        for carrier, cal in _CARRIER_RATES.items():
            premium = _fe_premium(request.age, face_value_dollars, request.tobacco, tier, cal)
            quotes.append(
                CarrierQuote(
                    carrier=carrier,
                    product_line=request.product_line,
                    monthly_premium=premium,
                    face_value=request.face_value,
                    rate_tier=tier,
                    source="formula",
                )
            )
        return sorted(quotes, key=lambda q: q.monthly_premium)
