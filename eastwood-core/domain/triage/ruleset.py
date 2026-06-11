"""
Ruleset loading. The default v0.1 ruleset is embedded here as a starting hypothesis.
All weights are PLACEHOLDERS — calibrate against real carrier guides per §10 of the spec.

Rules-as-data: store overrides in the `rulesets` table, keyed by version.
Every TriageResult stamps the version it used for provenance.
"""
from __future__ import annotations

DEFAULT_RULESET: dict = {
    "ruleset_version": "0.1",
    "effective_date": "2026-06-01",
    "max_issue_age": 85,
    "min_issue_age": 18,
    "thresholds": {
        "T1_call_now_ceiling": 0.35,
        "T2_workable_ceiling": 0.85,
    },
    "comorbidity_bump_per_extra_condition": 0.10,
    "comorbidity_bump_cap": 0.20,
    # ── L1 hard knockouts (nobody writes it) ─────────────────────────────────
    "hard_universal_decline": [
        "nursing_home_resident", "hospice_terminal", "currently_hospitalized",
        "dementia_alzheimers", "als", "huntingtons",
    ],
    # ── GI-floor conditions (workable only if agent has a GI carrier) ─────────
    # Severity is pushed to T2_workable_ceiling in L2; engine kills if no carrier.
    "gi_floor_conditions": [
        "cancer_stage4", "esrd_dialysis", "organ_transplant",
        "active_aids", "chronic_oxygen",
    ],
    # ── L2 condition weights (base severity, optional look-back window) ───────
    "conditions": [
        {"code": "hypertension_controlled",      "base_severity": 0.05, "lookback_years": None},
        {"code": "cholesterol_high_controlled",   "base_severity": 0.05, "lookback_years": None},
        {"code": "arthritis",                     "base_severity": 0.05, "lookback_years": None},
        {"code": "thyroid_stable",                "base_severity": 0.05, "lookback_years": None},
        {"code": "diabetes_oral_controlled",      "base_severity": 0.20, "lookback_years": None},
        {"code": "copd_mild",                     "base_severity": 0.45, "lookback_years": None},
        {"code": "diabetes_insulin",              "base_severity": 0.55, "lookback_years": None},
        {"code": "heart_attack",                  "base_severity": 0.60, "lookback_years": 2},
        {"code": "stroke_tia",                    "base_severity": 0.60, "lookback_years": 2},
        {"code": "cancer_remission",              "base_severity": 0.55, "lookback_years": 2},
        {"code": "chf",                           "base_severity": 0.75, "lookback_years": None},
        {"code": "copd_severe",                   "base_severity": 0.80, "lookback_years": None},
        {"code": "oxygen",                        "base_severity": 0.80, "lookback_years": None},
        {"code": "diabetes_with_complication",    "base_severity": 0.80, "lookback_years": None},
    ],
    # ── Medication → condition proxies ───────────────────────────────────────
    "med_proxies": [
        {"code": "insulin",         "implies": "diabetes_insulin",  "severity": 0.55},
        {"code": "warfarin",        "implies": "cardiac_clotting",  "severity": 0.50},
        {"code": "coumadin",        "implies": "cardiac_clotting",  "severity": 0.50},
        {"code": "eliquis",         "implies": "cardiac_clotting",  "severity": 0.50},
        {"code": "plavix",          "implies": "cardiac_clotting",  "severity": 0.50},
        {"code": "furosemide_high", "implies": "chf",               "severity": 0.70},
        {"code": "oxygen_therapy",  "implies": "copd_severe",       "severity": 0.55},
        {"code": "spiriva",         "implies": "copd_severe",       "severity": 0.55},
        {"code": "advair",          "implies": "copd_severe",       "severity": 0.55},
        # Dementia meds escalate back to L1 review — no severity assigned here
        {"code": "donepezil",       "implies": "dementia_alzheimers", "severity": None, "escalate_to_l1": True},
        {"code": "memantine",       "implies": "dementia_alzheimers", "severity": None, "escalate_to_l1": True},
        {"code": "oncology_agent",  "implies": "cancer_active",     "severity": 0.70},
    ],
    # ── Score modifiers ───────────────────────────────────────────────────────
    "modifiers": {
        "tobacco_current": 0.10,
        "tobacco_former":  0.03,
        "tobacco_unknown": 0.05,
        "bmi_obese_1":     0.05,   # 30 ≤ BMI < 35
        "bmi_obese_2":     0.10,   # 35 ≤ BMI < 40
        "bmi_obese_3":     0.15,   # BMI ≥ 40
        "age_75_80":       0.05,
        "age_81_plus":     0.10,
    },
    # ── Priority sort weights (must sum to 1.0) ───────────────────────────────
    "priority_weights": {
        "w_cleanliness":  0.30,
        "w_freshness":    0.30,
        "w_intent":       0.15,
        "w_attempts":     0.10,
        "w_replacement":  0.15,
    },
    # ── Attempt penalty (linear decay, capped) ───────────────────────────────
    "attempt_penalty_per_try": 0.12,
    "attempt_penalty_cap":     0.90,
    # ── Lookback decay factors ────────────────────────────────────────────────
    "lookback_outside_window_factor": 0.35,
    "lookback_unknown_date_factor":   1.0,
    # ── Source intent scores ──────────────────────────────────────────────────
    "source_intent": {
        "fb_lead_ad":         1.0,
        "facebook_leadgen":   1.0,
        "orphan":             0.80,
        "integrity_import":   0.65,
        "csv_import":         0.60,
        "aged_list":          0.40,
        "default":            0.50,
    },
    # ── Freshness half-life in days ───────────────────────────────────────────
    "freshness_halflife_days": 30,
    # ── Confidence: 5 fields required for a decisive score ───────────────────
    "confidence_critical_fields": ["age", "state", "tobacco_known", "health_data_present", "on_dnc"],
    # ── Contestability guard: min years before replacement is valid ───────────
    "contestability_window_years": 2,
}


def get_ruleset(db_payload: dict | None = None) -> dict:
    """Return active ruleset. DB payload overrides the default if provided."""
    if db_payload:
        # Deep-merge: db wins on top-level keys it provides
        merged = {**DEFAULT_RULESET, **db_payload}
        return merged
    return DEFAULT_RULESET
