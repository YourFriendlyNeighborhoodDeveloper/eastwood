"""
Carrier rate table seeder.
Generates rows programmatically from calibrated base rates.
Run once at startup (main.py checks if table is empty) or via:
  docker compose exec eastwood-core python -m scripts.seed_rate_tables
"""
from __future__ import annotations

from datetime import date

_EFFECTIVE = date(2025, 1, 1)

_FE_CARRIERS: dict[str, dict] = {
    "American Amicable": {"base": 3.75, "level": 1.00, "graded": 1.15, "gi": 1.35},
    "Mutual of Omaha":   {"base": 3.95, "level": 1.00, "graded": 1.12, "gi": 1.30},
    "Transamerica":      {"base": 3.65, "level": 1.00, "graded": 1.18, "gi": 1.40},
    "Prosperity Life":   {"base": 3.55, "level": 1.00, "graded": 1.10, "gi": 1.25},
    "Royal Neighbors":   {"base": 3.85, "level": 1.00, "graded": 1.14, "gi": 1.32},
}

_FE_FACE_VALUES_CENTS = [500_000, 1_000_000, 1_500_000, 2_000_000, 2_500_000]
_FE_TIERS = ["level", "graded", "gi"]


def _fe_premium_cents(age: int, fv_cents: int, tobacco: bool, tier: str, cal: dict) -> int:
    age_mult = 1.0 + max(0, age - 50) * 0.025
    tob_mult = 1.25 if tobacco else 1.0
    tier_mult = cal[tier]
    fv_k = fv_cents / 100_000  # face value in thousands of dollars
    premium = fv_k * cal["base"] * age_mult * tob_mult * tier_mult
    return max(int(round(premium * 100)), 999)  # min $9.99


def _fe_rows() -> list[dict]:
    rows = []
    for carrier, cal in _FE_CARRIERS.items():
        for age in range(45, 86):
            for tobacco in (False, True):
                for tier in _FE_TIERS:
                    for fv in _FE_FACE_VALUES_CENTS:
                        rows.append({
                            "carrier_name": carrier,
                            "product_line": "final_expense",
                            "rate_tier": tier,
                            "state": None,
                            "age": age,
                            "gender": None,
                            "tobacco": tobacco,
                            "face_value_cents": fv,
                            "monthly_premium_cents": _fe_premium_cents(age, fv, tobacco, tier, cal),
                            "effective_date": _EFFECTIVE,
                            "expires_date": None,
                            "is_active": True,
                        })
    return rows


_MEDICARE_MA = [
    ("Aetna",   "ma_plan", 2500,  "Aetna Medicare Advantage HMO"),
    ("Humana",  "ma_plan", 1800,  "Humana Medicare Advantage PPO"),
]

_MEDICARE_SUPP = [
    # (carrier, plan_tier, base_premium_cents_at_65, annual_increase_pct)
    ("Aetna",   "plan_g", 9500,  4.0),
    ("Aetna",   "plan_n", 7800,  4.0),
    ("Humana",  "plan_g", 9800,  4.0),
    ("Humana",  "plan_n", 8200,  4.0),
]


def _medicare_rows() -> list[dict]:
    rows = []
    # Medicare Advantage — not age-rated, one row per age bracket for lookup
    for carrier, tier, premium_cents, _ in _MEDICARE_MA:
        for age in range(65, 90):
            rows.append({
                "carrier_name": carrier,
                "product_line": "medicare_advantage",
                "rate_tier": tier,
                "state": None,
                "age": age,
                "gender": None,
                "tobacco": False,
                "face_value_cents": 0,
                "monthly_premium_cents": premium_cents,
                "effective_date": _EFFECTIVE,
                "expires_date": None,
                "is_active": True,
            })

    # Medicare Supplement — age-rated
    for carrier, tier, base_cents, pct in _MEDICARE_SUPP:
        for age in range(65, 90):
            age_factor = 1.0 + (age - 65) * (pct / 100)
            premium_cents = int(round(base_cents * age_factor))
            rows.append({
                "carrier_name": carrier,
                "product_line": "medicare_supplement",
                "rate_tier": tier,
                "state": None,
                "age": age,
                "gender": None,
                "tobacco": False,
                "face_value_cents": 0,
                "monthly_premium_cents": premium_cents,
                "effective_date": _EFFECTIVE,
                "expires_date": None,
                "is_active": True,
            })
    return rows


def _all_seed_rows() -> list[dict]:
    return _fe_rows() + _medicare_rows()


if __name__ == "__main__":
    import asyncio
    import sys
    import os

    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

    async def _run():
        from db.session import _get_engine
        from db.models import CarrierRateTable
        from sqlalchemy import delete

        rows = _all_seed_rows()
        async with _get_engine().begin() as conn:
            await conn.execute(delete(CarrierRateTable))
            await conn.execute(CarrierRateTable.__table__.insert(), rows)
        print(f"Seeded {len(rows)} rate table rows.")

    asyncio.run(_run())
