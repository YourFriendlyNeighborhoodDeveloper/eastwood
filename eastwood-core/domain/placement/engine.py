"""
Carrier placement engine.

Given a lead's health data and triage result, returns a ranked list of carriers
with the rate tier each would offer and the exact questions still needed.

This is field underwriting — the system pre-qualifies before a human spends
time on the call, and tells the AI agent exactly what to ask per carrier.
"""
from __future__ import annotations

from dataclasses import dataclass

from .carriers import CARRIERS, CarrierProfile


def _profiles_from_db(db_rows: list) -> list[CarrierProfile]:
    """Convert CarrierProfileModel ORM rows to CarrierProfile dataclasses."""
    out: list[CarrierProfile] = []
    for row in db_rows:
        out.append(CarrierProfile(
            name=row.carrier_name,
            products=[row.product_line],
            states=row.states,
            min_age=row.min_age,
            max_age=row.max_age,
            gi_carrier=row.has_gi,
            conditions_accepted=list(row.level_conditions or []),
            conditions_graded=list(row.graded_conditions or []),
            required_questions=list(row.required_questions or []),
            priority_weight=float(row.priority_weight),
        ))
    return out

# Map condition code → carrier-specific follow-up questions needed
CONDITION_QUESTIONS: dict[str, list[str]] = {
    "diabetes_oral_controlled": ["diabetes_type", "a1c", "complications"],
    "diabetes_insulin":         ["diabetes_type", "a1c", "complications", "diagnosis_date"],
    "diabetes_with_complication": ["diabetes_type", "a1c", "complications", "diagnosis_date"],
    "heart_attack":             ["heart_attack_date", "stents", "bypass", "current_meds"],
    "stroke_tia":               ["stroke_date", "residual_deficits", "current_meds"],
    "cancer_remission":         ["cancer_type", "treatment_end_date", "remission_confirmed"],
    "cancer_stage4":            ["cancer_type", "treatment_status"],
    "copd_mild":                ["oxygen_use", "fev1", "hospitalizations_past_year"],
    "copd_severe":              ["oxygen_use", "fev1", "hospitalizations_past_year"],
    "chf":                      ["ejection_fraction", "hospitalizations_past_year", "current_meds"],
    "esrd_dialysis":            ["dialysis_frequency", "transplant_listed"],
    "oxygen":                   ["oxygen_use", "lpm", "diagnosis"],
    "chronic_oxygen":           ["oxygen_use", "lpm", "diagnosis"],
}


@dataclass
class PlacementOption:
    carrier: str
    product: str
    rate_tier: str              # level | graded | modified | gi
    confidence: float           # 0-1: how likely this placement succeeds
    monthly_premium_est: float  # rough estimate based on tier + age
    required_questions: list[str]
    missing_questions: list[str]  # subset of required not yet captured
    placement_notes: str


@dataclass
class PlacementResult:
    lead_id: str
    options: list[PlacementOption]   # ranked best → worst
    top_carrier: str | None
    top_tier: str | None
    all_required_questions: list[str]  # union of questions needed across top options
    missing_questions: list[str]       # what the agent still needs to collect
    gi_available: bool
    placement_ready: bool              # True when no missing_questions remain


_TIER_PREMIUM_MULTIPLIER = {
    "level":    1.0,
    "graded":   1.35,
    "modified": 1.60,
    "gi":       2.10,
}


def _estimate_premium(age: int | None, face_value: int, tier: str, tobacco: str) -> float:
    age = age or 65
    base_rate = (face_value / 1000) * (2.0 + max(0, age - 50) * 0.08)
    tier_mult = _TIER_PREMIUM_MULTIPLIER.get(tier, 1.0)
    tobacco_mult = 1.20 if tobacco in ("current", "unknown") else 1.0
    return round(base_rate * tier_mult * tobacco_mult, 2)


def _rate_tier(
    carrier: CarrierProfile,
    condition_codes: set[str],
    medication_codes: set[str],
) -> str:
    gi_conditions = {"cancer_stage4", "esrd_dialysis", "organ_transplant", "active_aids", "chronic_oxygen"}

    if condition_codes & gi_conditions:
        return "gi" if carrier.gi_carrier else "declined"

    graded_hit = condition_codes & set(carrier.conditions_graded)
    if graded_hit:
        return "graded"

    accepted_hit = condition_codes & set(carrier.conditions_accepted)
    # Conditions not in accepted or graded lists = modified or declined
    unknown = condition_codes - set(carrier.conditions_accepted) - set(carrier.conditions_graded) - gi_conditions
    if unknown:
        return "modified"

    return "level"


def _missing_for_conditions(
    condition_codes: set[str],
    already_captured: set[str],
    carrier_required: list[str],
) -> list[str]:
    needed: set[str] = set(carrier_required)
    for code in condition_codes:
        for q in CONDITION_QUESTIONS.get(code, []):
            needed.add(q)
    return sorted(needed - already_captured)


def place(
    lead_id: str,
    age: int | None,
    state: str | None,
    product_line: str | None,
    condition_codes: set[str],
    medication_codes: set[str],
    tobacco: str,
    face_value: int = 10_000,
    already_captured: set[str] | None = None,
    triage_bucket: str | None = None,
    db_carrier_profiles: list | None = None,  # CarrierProfileModel rows; falls back to hardcoded
) -> PlacementResult:
    """
    Rank carriers for this lead. Returns placement options sorted by confidence.
    Call this after SAURON triage; feed the result into the acquisition plan.
    """
    captured = already_captured or set()
    target_product = product_line or "final_expense"
    roster = _profiles_from_db(db_carrier_profiles) if db_carrier_profiles else CARRIERS

    options: list[PlacementOption] = []

    for carrier in roster:
        if target_product not in carrier.products:
            continue
        if state and carrier.states and state.upper() not in carrier.states:
            continue
        if age and (age < carrier.min_age or age > carrier.max_age):
            continue

        tier = _rate_tier(carrier, condition_codes, medication_codes)
        if tier == "declined":
            continue

        # Confidence: level=high, graded=medium, modified=low, gi=very low
        tier_confidence = {"level": 0.85, "graded": 0.60, "modified": 0.35, "gi": 0.20}
        confidence = round(tier_confidence.get(tier, 0.1) * carrier.priority_weight, 3)

        missing = _missing_for_conditions(condition_codes, captured, carrier.required_questions)
        premium_est = _estimate_premium(age, face_value, tier, tobacco)

        notes_map = {
            "level":    "clean health — level benefit, lowest premium",
            "graded":   "graded benefit: 30%/70%/100% payout years 1/2/3+",
            "modified": "modified benefit: returns premium + interest years 1-2",
            "gi":       "guaranteed issue — no health questions, 2-year waiting period",
        }

        options.append(PlacementOption(
            carrier=carrier.name,
            product=target_product,
            rate_tier=tier,
            confidence=confidence,
            monthly_premium_est=premium_est,
            required_questions=carrier.required_questions,
            missing_questions=missing,
            placement_notes=notes_map.get(tier, ""),
        ))

    options.sort(key=lambda o: o.confidence, reverse=True)

    # Aggregate missing questions across top 3 options
    top_options = options[:3]
    all_missing: set[str] = set()
    all_required: set[str] = set()
    for opt in top_options:
        all_missing.update(opt.missing_questions)
        all_required.update(opt.required_questions)

    gi_available = any(o.rate_tier == "gi" for o in options)

    return PlacementResult(
        lead_id=lead_id,
        options=options,
        top_carrier=options[0].carrier if options else None,
        top_tier=options[0].rate_tier if options else None,
        all_required_questions=sorted(all_required),
        missing_questions=sorted(all_missing),
        gi_available=gi_available,
        placement_ready=len(all_missing) == 0,
    )
