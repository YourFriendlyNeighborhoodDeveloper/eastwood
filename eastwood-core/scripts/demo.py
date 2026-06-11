#!/usr/bin/env python3
"""
Triage engine demo — 12 cases covering every decision path.

Paths exercised:
  L0-DNC      DNC with no consent → DEAD
  L0-LITIG    Known litigator → DEAD
  L0-PHONE    Invalid phone → DEAD
  L0-AGE      Age 88 → DEAD (max_issue_age)
  L1-HARD     Dementia (hard knockout) → DEAD
  L1-MED      Donepezil (escalates to dementia) → DEAD
  GI-CARRIER  ESRD dialysis, GI carrier available → WORKABLE (at T2 ceiling)
  GI-NO-CARR  ESRD dialysis, no GI carrier → DEAD
  CALL-NOW    Age 52, tobacco never, no conditions → CALL_NOW
  WORKABLE    CHF + insulin → WORKABLE
  LOOKBACK    Heart attack 4 years ago (outside 2yr window, decayed) → CALL_NOW
  ENRICH      No age, no state, no health info → CALL_NOW + needs_enrichment

Run:
  python scripts/demo.py
"""
from __future__ import annotations

import json
import pathlib
import sys
import uuid

_ROOT = pathlib.Path(__file__).parent.parent
# Insert domain/ so triage.* is importable without triggering domain/__init__.py
sys.path.insert(0, str(_ROOT / "domain"))
sys.path.insert(0, str(_ROOT))

from triage.ruleset import DEFAULT_RULESET

_RULESET_JSON = _ROOT / "domain" / "triage" / "ruleset_v0_1.json"
RULESET = json.loads(_RULESET_JSON.read_text()) if _RULESET_JSON.exists() else DEFAULT_RULESET


def _lead(state="TX", age=55, source="fb_lead_ad", on_dnc=False, consent=False, **extra) -> dict:
    return {"lead_id": str(uuid.uuid4()), "source": source, "state": state,
            "age": age, "on_dnc": on_dnc, "express_written_consent": consent, **extra}


CASES = [
    # ── L0 gates ──────────────────────────────────────────────────────────────
    {
        "name": "L0-DNC",
        "desc": "DNC with no consent → DEAD",
        "lead": _lead(on_dnc=True, consent=False),
        "health": {},
        "gi_carrier": True,
        "expect_bucket": "DEAD",
        "expect_reason": "dnc_no_consent",
    },
    {
        "name": "L0-LITIG",
        "desc": "Known litigator → DEAD",
        "lead": _lead(),
        "health": {"known_litigator": True},
        "gi_carrier": True,
        "expect_bucket": "DEAD",
        "expect_reason": "litigator_quarantine",
    },
    {
        "name": "L0-PHONE",
        "desc": "Invalid phone → DEAD",
        "lead": _lead(),
        "health": {"phone_valid": False},
        "gi_carrier": True,
        "expect_bucket": "DEAD",
        "expect_reason": "no_contact",
    },
    {
        "name": "L0-AGE",
        "desc": "Age 88 exceeds max_issue_age 85 → DEAD",
        "lead": _lead(age=88),
        "health": {},
        "gi_carrier": True,
        "expect_bucket": "DEAD",
        "expect_reason": "age_out",
    },
    # ── L1 gates ──────────────────────────────────────────────────────────────
    {
        "name": "L1-HARD",
        "desc": "Dementia alzheimers (hard knockout) → DEAD",
        "lead": _lead(age=74),
        "health": {"tobacco": "none", "conditions": [{"code": "dementia_alzheimers", "raw_text": "dementia"}]},
        "gi_carrier": True,
        "expect_bucket": "DEAD",
        "expect_reason": "l1_hard_knockout",
    },
    {
        "name": "L1-MED",
        "desc": "Donepezil (escalates to dementia) → DEAD",
        "lead": _lead(age=71),
        "health": {"tobacco": "none", "medications": [{"code": "donepezil", "raw_text": "Aricept"}]},
        "gi_carrier": True,
        "expect_bucket": "DEAD",
        "expect_reason": "l1_hard_knockout",
    },
    # ── GI-floor ──────────────────────────────────────────────────────────────
    {
        "name": "GI-CARRIER",
        "desc": "ESRD dialysis with GI carrier → WORKABLE at T2 ceiling",
        "lead": _lead(age=62),
        "health": {"tobacco": "none", "conditions": [{"code": "esrd_dialysis", "raw_text": "dialysis"}]},
        "gi_carrier": True,
        "expect_bucket": "WORKABLE",
        "expect_reason": None,
    },
    {
        "name": "GI-NO-CARR",
        "desc": "ESRD dialysis with no GI carrier → DEAD",
        "lead": _lead(age=62),
        "health": {"tobacco": "none", "conditions": [{"code": "esrd_dialysis", "raw_text": "dialysis"}]},
        "gi_carrier": False,
        "expect_bucket": "DEAD",
        "expect_reason": "gi_floor_no_carrier",
    },
    # ── L2 scoring ────────────────────────────────────────────────────────────
    {
        "name": "CALL-NOW",
        "desc": "Age 52, non-tobacco, clean → CALL_NOW",
        "lead": _lead(age=52),
        "health": {"tobacco": "none"},
        "gi_carrier": True,
        "expect_bucket": "CALL_NOW",
        "expect_reason": None,
    },
    {
        "name": "WORKABLE",
        "desc": "CHF + insulin (high severity) → WORKABLE",
        "lead": _lead(age=67),
        "health": {
            "tobacco": "current",
            "conditions": [{"code": "chf", "raw_text": "congestive heart failure"}],
            "medications": [{"code": "insulin", "raw_text": "insulin glargine"}],
        },
        "gi_carrier": True,
        "expect_bucket": "WORKABLE",
        "expect_reason": None,
    },
    {
        "name": "LOOKBACK",
        "desc": "Heart attack 4 years ago (outside 2yr window) → severity decayed → CALL_NOW",
        "lead": _lead(age=58),
        "health": {
            "tobacco": "none",
            "conditions": [{"code": "heart_attack", "raw_text": "MI", "last_event_on": "2022-01-01"}],
        },
        "gi_carrier": True,
        "expect_bucket": "CALL_NOW",
        "expect_reason": None,
    },
    {
        "name": "ENRICH",
        "desc": "No age, no state, no health info → scored but needs_enrichment=True",
        "lead": {"lead_id": str(uuid.uuid4()), "source": "aged_list", "on_dnc": False, "express_written_consent": False},
        "health": {},
        "gi_carrier": True,
        "expect_bucket": None,  # any bucket is fine — just check needs_enrichment
        "expect_needs_enrichment": True,
    },
]


def _run_case(case: dict) -> tuple[bool, str]:
    from triage.models import TriageInput, Compliance, Condition, Medication, TobaccoStatus

    hd = case["health"]
    conditions = [Condition(**c) for c in hd.get("conditions", [])]
    medications = [Medication(**m) for m in hd.get("medications", [])]
    compliance = Compliance(
        on_dnc=case["lead"].get("on_dnc"),
        has_express_written_consent=bool(case["lead"].get("express_written_consent")),
        known_litigator=bool(hd.get("known_litigator", False)),
        phone_valid=hd.get("phone_valid"),
        is_the_insured=hd.get("is_the_insured"),
    )
    tobacco_raw = hd.get("tobacco", "unknown").lower()
    try:
        tobacco = TobaccoStatus(tobacco_raw)
    except ValueError:
        tobacco = TobaccoStatus.UNKNOWN

    inp = TriageInput(
        lead_id=str(case["lead"].get("lead_id", "")),
        source=str(case["lead"].get("source", "unknown")),
        state=case["lead"].get("state"),
        age=case["lead"].get("age"),
        tobacco=tobacco,
        conditions=conditions,
        medications=medications,
        compliance=compliance,
    )

    from triage.engine import triage
    result = triage(inp, ruleset=RULESET, agent_has_gi_carrier=case.get("gi_carrier", True))

    checks: list[str] = []
    passed = True

    expected_bucket = case.get("expect_bucket")
    if expected_bucket and result.bucket != expected_bucket:
        checks.append(f"bucket={result.bucket!r} want {expected_bucket!r}")
        passed = False

    expected_reason = case.get("expect_reason")
    if expected_reason is not None and result.dead_reason != expected_reason:
        checks.append(f"dead_reason={result.dead_reason!r} want {expected_reason!r}")
        passed = False

    if case.get("expect_needs_enrichment") and not result.needs_enrichment:
        checks.append("needs_enrichment=False want True")
        passed = False

    summary = (
        f"bucket={result.bucket} sev={result.severity_score:.3f} "
        f"pri={result.priority:.3f} conf={result.confidence:.2f} enrich={result.needs_enrichment}"
    )
    if not passed:
        summary += "  FAIL: " + "; ".join(checks)

    drivers_str = "\n      ".join(result.drivers[:4])
    detail = f"  drivers: {drivers_str}"
    return passed, f"{summary}\n{detail}"


def main() -> None:
    passed_total = 0
    failed_total = 0

    print(f"\n{'─'*70}")
    print("  Eastwood Triage Engine — Demo (12 paths)")
    print(f"  Ruleset: {RULESET['ruleset_version']}  |  T1={RULESET['thresholds']['T1_call_now_ceiling']}  T2={RULESET['thresholds']['T2_workable_ceiling']}")
    print(f"{'─'*70}\n")

    for case in CASES:
        ok, detail = _run_case(case)
        status_icon = "PASS" if ok else "FAIL"
        print(f"[{status_icon}] {case['name']:<14}  {case['desc']}")
        print(f"      {detail}\n")
        if ok:
            passed_total += 1
        else:
            failed_total += 1

    print(f"{'─'*70}")
    print(f"  {passed_total}/{len(CASES)} passed   {failed_total} failed")
    print(f"{'─'*70}\n")
    sys.exit(0 if failed_total == 0 else 1)


if __name__ == "__main__":
    main()
