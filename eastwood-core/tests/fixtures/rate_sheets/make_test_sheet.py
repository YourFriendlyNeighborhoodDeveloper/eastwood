"""Write the SYNTHETIC acceptance-test rate sheet (not carrier data).

    python -m tests.fixtures.rate_sheets.make_test_sheet <out_dir>

Produces rates.csv, meta.json (is_test: true — production refuses it) and
checks.csv: expected premiums computed here, independently of the engine, from
the same published-style formula a carrier rate book uses:
    monthly = round((rate_per_1000_annual x face / 1000 + policy_fee) x 0.0875, 2)
"""
from __future__ import annotations

import csv
import json
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

POLICY_FEE = Decimal("36.00")
MONTHLY = Decimal("0.0875")
META = {
    "carrier": "Synthetic Test Life (NOT A REAL CARRIER)",
    "product_line": "final_expense",
    "plan_name": "Test Final Expense",
    "states": ["MI", "OH", "WI", "IN", "IL", "FL", "TX"],
    "effective_date": "2026-01-01",
    "expires_date": "2027-07-01",
    "rate_unit": "per_1000_annual",
    "policy_fee_annual": str(POLICY_FEE),
    "modal_factors": {"annual": "1.0", "semiannual": "0.51", "quarterly": "0.26", "monthly": str(MONTHLY)},
    "rounding": "final",
    "min_face": 2000, "max_face": 40000, "face_increment": 1000,
    "default_tier": "level",
    "tier_questions": [{"key": "major_2y", "yes_tier": "graded",
                        "text": "In the past 2 years, have you been diagnosed with or treated for cancer, a heart attack or a stroke?"}],
    "source_document": "SYNTHETIC acceptance-test rate book (tests/fixtures/rate_sheets/make_test_sheet.py). Not carrier data.",
    "is_test": True,
}


def rate(age: int, sex: str, tobacco: bool, tier: str) -> Decimal:
    x = age - 50
    base = Decimal(18) + Decimal("1.6") * x + Decimal("0.06") * x * x
    base *= Decimal("1.0") if sex == "F" else Decimal("1.18")
    base *= Decimal("1.4") if tobacco else Decimal("1.0")
    base *= Decimal("1.0") if tier == "level" else Decimal("1.45")
    return base.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def monthly(age, sex, tobacco, tier, face) -> Decimal:
    annual = rate(age, sex, tobacco, tier) * Decimal(face) / 1000 + POLICY_FEE
    return (annual * MONTHLY).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def write(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "rates.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["age", "sex", "tobacco", "tier", "rate"])
        for age in range(50, 86):
            for sex in ("M", "F"):
                for tob in (False, True):
                    for tier in ("level", "graded"):
                        w.writerow([age, sex, "Y" if tob else "N", tier, rate(age, sex, tob, tier)])
    (out / "meta.json").write_text(json.dumps(META, indent=2))
    with open(out / "checks.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["age", "sex", "tobacco", "tier", "face", "mode", "expected"])
        for age, sex, tob, tier, face in ((65, "F", False, "level", 10000), (72, "M", True, "level", 15000),
                                          (58, "F", False, "graded", 20000), (80, "M", False, "level", 5000),
                                          (68, "F", True, "level", 12000), (61, "M", True, "graded", 8000),
                                          (77, "M", False, "graded", 10000), (70, "F", True, "graded", 15000)):
            w.writerow([age, sex, "Y" if tob else "N", tier, face, "monthly", monthly(age, sex, tob, tier, face)])


if __name__ == "__main__":
    write(Path(sys.argv[1] if len(sys.argv) > 1 else "."))
