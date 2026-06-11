"""
Carrier profiles for final expense and Medicare products.

Each profile encodes what a carrier will/won't write and what questions
the agent must answer before a placement decision can be made.
Weights are calibrated against typical field-underwriting guides —
update when you have actual carrier contracts in hand.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CarrierProfile:
    name: str
    products: list[str]               # final_expense | medicare_supplement | medicare_advantage | aca | life
    states: list[str] | None          # None = all licensed states
    min_age: int
    max_age: int
    gi_carrier: bool                  # writes guaranteed-issue products
    # Condition codes this carrier will accept at level/graded rates
    conditions_accepted: list[str] = field(default_factory=list)
    # Condition codes that will push to modified/GI — not auto-decline
    conditions_graded: list[str] = field(default_factory=list)
    # Health questions required before a quote is valid for this carrier
    required_questions: list[str] = field(default_factory=list)
    # Relative close-rate weight for sorting (higher = dial first)
    priority_weight: float = 1.0


# ── Final expense carrier roster ──────────────────────────────────────────────

CARRIERS: list[CarrierProfile] = [
    CarrierProfile(
        name="American Amicable",
        products=["final_expense"],
        states=None,
        min_age=45, max_age=85,
        gi_carrier=False,
        conditions_accepted=[
            "hypertension_controlled", "cholesterol_high_controlled",
            "arthritis", "thyroid_stable", "diabetes_oral_controlled",
        ],
        conditions_graded=[
            "diabetes_insulin", "copd_mild", "cancer_remission",
            "heart_attack", "stroke_tia",
        ],
        required_questions=["tobacco", "diabetes_type", "heart_attack_date", "cancer_type"],
        priority_weight=1.2,
    ),
    CarrierProfile(
        name="Mutual of Omaha",
        products=["final_expense"],
        states=None,
        min_age=45, max_age=85,
        gi_carrier=False,
        conditions_accepted=[
            "hypertension_controlled", "cholesterol_high_controlled",
            "arthritis", "thyroid_stable",
        ],
        conditions_graded=[
            "diabetes_oral_controlled", "copd_mild",
        ],
        required_questions=["tobacco", "height_weight", "diabetes_type", "hospitalization_recent"],
        priority_weight=1.3,
    ),
    CarrierProfile(
        name="Royal Neighbors",
        products=["final_expense"],
        states=None,
        min_age=50, max_age=85,
        gi_carrier=False,
        conditions_accepted=[
            "hypertension_controlled", "cholesterol_high_controlled",
            "arthritis", "thyroid_stable", "diabetes_oral_controlled",
            "diabetes_insulin",
        ],
        conditions_graded=[
            "copd_mild", "cancer_remission", "heart_attack", "stroke_tia",
        ],
        required_questions=["tobacco", "diabetes_type", "a1c", "cancer_type"],
        priority_weight=1.1,
    ),
    CarrierProfile(
        name="Transamerica",
        products=["final_expense"],
        states=None,
        min_age=18, max_age=80,
        gi_carrier=True,
        conditions_accepted=[
            "hypertension_controlled", "cholesterol_high_controlled",
            "arthritis", "thyroid_stable", "diabetes_oral_controlled",
            "diabetes_insulin", "copd_mild",
        ],
        conditions_graded=[
            "cancer_remission", "heart_attack", "stroke_tia",
            "chf", "copd_severe", "diabetes_with_complication",
            "cancer_stage4", "esrd_dialysis", "organ_transplant",
            "active_aids", "chronic_oxygen",
        ],
        required_questions=["tobacco", "height_weight", "diabetes_type", "oxygen_use"],
        priority_weight=0.9,
    ),
    CarrierProfile(
        name="Prosperity Life",
        products=["final_expense"],
        states=None,
        min_age=40, max_age=85,
        gi_carrier=True,
        conditions_accepted=[
            "hypertension_controlled", "cholesterol_high_controlled",
            "arthritis", "thyroid_stable", "diabetes_oral_controlled",
            "diabetes_insulin", "copd_mild", "chf",
        ],
        conditions_graded=[
            "cancer_remission", "heart_attack", "stroke_tia",
            "copd_severe", "diabetes_with_complication",
            "cancer_stage4", "esrd_dialysis", "organ_transplant",
            "active_aids", "chronic_oxygen",
        ],
        required_questions=["tobacco", "oxygen_use", "hospitalization_recent"],
        priority_weight=0.95,
    ),
    CarrierProfile(
        name="Aetna / CVS",
        products=["medicare_supplement", "medicare_advantage"],
        states=None,
        min_age=65, max_age=99,
        gi_carrier=False,
        conditions_accepted=[
            "hypertension_controlled", "cholesterol_high_controlled",
            "arthritis", "thyroid_stable", "diabetes_oral_controlled",
            "diabetes_insulin",
        ],
        conditions_graded=[],
        required_questions=["part_a_part_b", "current_meds", "tobacco"],
        priority_weight=1.1,
    ),
    CarrierProfile(
        name="Humana",
        products=["medicare_advantage", "medicare_supplement"],
        states=None,
        min_age=65, max_age=99,
        gi_carrier=False,
        conditions_accepted=[
            "hypertension_controlled", "cholesterol_high_controlled",
            "arthritis", "thyroid_stable", "diabetes_oral_controlled",
            "diabetes_insulin", "copd_mild",
        ],
        conditions_graded=[],
        required_questions=["part_a_part_b", "preferred_doctors", "drug_list"],
        priority_weight=1.0,
    ),
]
